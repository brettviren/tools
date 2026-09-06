"""kitty-open: push a remote file/dir to the local machine and open it there."""

import fnmatch
import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import click

log = logging.getLogger("kitty-open")

CONFIG_DIR = Path.home() / ".config" / "kitty-open"
CONFIG_FILE = CONFIG_DIR / "config.py"
PASS_FILE = CONFIG_DIR / "transfer.pass"
DEFAULT_STAGE_ROOT = Path.home() / ".cache" / "kitty-open" / "stage"
STAGE_ROOT_REMOTE_LITERAL = "~/.cache/kitty-open/stage"  # sent as-is to kitten/kitty

# Where this same script lives on the LOCAL machine. `kitty @ launch` execs
# directly (no shell, no PATH re-resolution via .bashrc/.profile), so a bare
# "kitty-open" is only found if ~/.local/bin happens to already be in
# whatever PATH kitty itself started with -- often not true for a WM-spawned
# kitty. We route the relaunch through /bin/sh -c instead, so "~" still
# expands against the LOCAL $HOME but we don't depend on kitty's PATH.
LOCAL_SCRIPT = "~/.local/bin/kitty-open"

DEFAULT_CONFIG = '''\
# kitty-open local configuration. This file is exec()'d, so it's just Python.

# (host_glob, remote_path_glob, local_dir) -- first match wins.
# local_dir is created if missing. No match => file stays in the stage dir.
MAPPINGS = [
    # ("*", "/home/bv/work/A/*", "~/dropbox/A"),
    # ("*", "/home/bv/work/B/*", "/path/to/big/disk/B"),
]

# extension (lowercase, with dot) or the special key "directory" -> command.
DISPATCH = {
    "directory": "xdg-open",
}

DEFAULT_APP = "xdg-open"
'''


# ---------------------------------------------------------------------------
# Library functions
# ---------------------------------------------------------------------------

def setup_logging(level_name: str) -> None:
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        raise click.BadParameter(f"invalid log level: {level_name!r}")
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    log.debug("logging initialized at level=%s pid=%d argv=%r", level_name.upper(), os.getpid(), sys.argv)
    log.debug("KITTY_LISTEN_ON=%r  KITTY_WINDOW_ID=%r  SSH_CONNECTION=%r",
              os.environ.get("KITTY_LISTEN_ON"), os.environ.get("KITTY_WINDOW_ID"),
              os.environ.get("SSH_CONNECTION"))


def load_local_config() -> dict:
    ns = {"MAPPINGS": [], "DISPATCH": {}, "DEFAULT_APP": "xdg-open"}
    if CONFIG_FILE.exists():
        code = CONFIG_FILE.read_text()
        exec(compile(code, str(CONFIG_FILE), "exec"), ns)  # noqa: S102 -- config is deliberately just Python
    return ns


def sanitize(remote_path: str) -> str:
    return remote_path.strip("/").replace("/", "-")


def stage_name(host: str, remote_path: str) -> str:
    return f"{host}__{sanitize(remote_path)}"


def find_dotfile(start: Path) -> dict:
    """Walk start's directory and its parents for a .kitty-open key=value file."""
    d = (start if start.is_dir() else start.parent).resolve()
    for parent in [d, *d.parents]:
        f = parent / ".kitty-open"
        if f.exists():
            out = {}
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
            log.debug("found .kitty-open at %s: %r", f, out)
            return out
    log.debug("no .kitty-open found above %s", d)
    return {}


def local_launch(argv: list[str]) -> subprocess.CompletedProcess:
    """Run argv on the LOCAL machine via the forwarded remote-control socket,
    through /bin/sh -c so a leading '~' in any argv[0] path expands correctly
    against the local $HOME, without depending on kitty's own PATH."""
    shell_cmd = " ".join(shlex.quote(a) if not a.startswith("~") else a for a in argv)
    launch_cmd = ["kitty", "@", "launch", "--type=background", "--dont-take-focus", "--",
                  "/bin/sh", "-c", shell_cmd]
    log.debug("local_launch: %r", launch_cmd)
    result = subprocess.run(launch_cmd, capture_output=True, text=True, check=False)
    log.debug("local_launch returncode=%d stdout=%r stderr=%r",
              result.returncode, result.stdout.strip(), result.stderr.strip())
    if result.returncode != 0:
        log.warning("'kitty @ launch' failed (rc=%d): %s -- is forward_remote_control "
                     "active for this host, and KITTY_LISTEN_ON set? (see debug log above)",
                     result.returncode, result.stderr.strip())
    return result


# ---------------------------------------------------------------- push mode

def run_push(
    paths: list[str],
    host: str | None,
    dest: str | None,
    app: str | None,
    no_open: bool,
) -> None:
    host = host or socket.gethostname()
    log.debug("run_push: host=%r paths=%r", host, paths)

    bypass_arg = []
    if PASS_FILE.exists():
        bypass_arg = ["--permissions-bypass", str(PASS_FILE)]
        log.debug("using bypass password file %s", PASS_FILE)
    else:
        log.debug("no bypass password file at %s -- expect a confirmation popup", PASS_FILE)

    for raw_path in paths:
        remote_path = str(Path(raw_path).resolve())
        log.debug("pushing %s", remote_path)
        if not Path(remote_path).exists():
            log.error("no such path: %s", remote_path)
            continue

        cfg = find_dotfile(Path(remote_path))
        dest_override = dest or cfg.get("dest")
        app_override = app or cfg.get("app")
        is_dir = Path(remote_path).is_dir()
        name = stage_name(host, remote_path)
        log.debug("dest_override=%r app_override=%r is_dir=%s stage_name=%r",
                  dest_override, app_override, is_dir, name)

        if is_dir:
            stage_path = f"{STAGE_ROOT_REMOTE_LITERAL}/{name}/"
            # The dest dir must already exist on the LOCAL side before a
            # directory transfer.
            log.debug("pre-creating remote-side stage dir %r via local_launch", stage_path)
            local_launch(["mkdir", "-p", stage_path])
            time.sleep(0.3)  # fire-and-forget guard, see module docstring
        else:
            stage_path = f"{STAGE_ROOT_REMOTE_LITERAL}/{name}"
        log.debug("stage_path=%r", stage_path)

        transfer_cmd = ["kitten", "transfer", *bypass_arg, remote_path, stage_path]
        log.debug("transferring to %s", stage_path)
        log.debug("transfer_cmd=%r", transfer_cmd)
        result = subprocess.run(transfer_cmd, check=False)
        log.debug("kitten transfer returncode=%d", result.returncode)
        if result.returncode != 0:
            log.error("transfer failed for %s (rc=%d)", remote_path, result.returncode)
            continue

        if no_open:
            log.debug("--no-open set, skipping dispatch")
            continue

        dispatch_args = [LOCAL_SCRIPT, "--dispatch", stage_path, host, remote_path]
        if dest_override:
            dispatch_args += ["--dest", dest_override]
        if app_override:
            dispatch_args += ["--app", app_override]
        log.debug("dispatching open on local machine")
        local_launch(dispatch_args)


# ------------------------------------------------------------ dispatch mode

def guess_app(path: Path, cfg: dict) -> str:
    if path.is_dir():
        return cfg["DISPATCH"].get("directory", cfg["DEFAULT_APP"])
    return cfg["DISPATCH"].get(path.suffix.lower(), cfg["DEFAULT_APP"])


def run_dispatch(stage_path: str, remote_host: str, remote_path: str,
                  dest_override: str | None, app_override: str | None) -> None:
    log.debug("run_dispatch: stage_path=%r remote_host=%r remote_path=%r dest_override=%r app_override=%r",
              stage_path, remote_host, remote_path, dest_override, app_override)
    cfg = load_local_config()
    log.debug("local config: MAPPINGS=%r DISPATCH=%r DEFAULT_APP=%r",
              cfg["MAPPINGS"], cfg["DISPATCH"], cfg["DEFAULT_APP"])
    stage = Path(os.path.expanduser(stage_path))
    if not stage.exists():
        log.error("staged path does not exist locally: %s", stage)

    final_dir = None
    if dest_override:
        final_dir = Path(os.path.expanduser(dest_override))
        log.debug("using --dest override: %s", final_dir)
    else:
        for host_glob, path_glob, local_dir in cfg["MAPPINGS"]:
            if fnmatch.fnmatch(remote_host, host_glob) and fnmatch.fnmatch(remote_path, path_glob):
                final_dir = Path(os.path.expanduser(local_dir))
                log.debug("MAPPINGS match (%r, %r) -> %s", host_glob, path_glob, final_dir)
                break
        else:
            log.debug("no MAPPINGS match for host=%r path=%r, staying in stage dir",
                      remote_host, remote_path)

    if final_dir is not None:
        final_dir.mkdir(parents=True, exist_ok=True)
        final_path = final_dir / stage.name
        if stage.resolve() != final_path.resolve():
            if final_path.exists():
                shutil.rmtree(final_path) if final_path.is_dir() else final_path.unlink()
            shutil.move(str(stage), str(final_path))
    else:
        final_path = stage
    log.debug("final_path=%s", final_path)

    app = app_override or guess_app(final_path, cfg)
    log.debug("opening %s with %r", final_path, app)
    try:
        subprocess.Popen(
            [app, str(final_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        log.exception("failed to launch %r on %s", app, final_path)


# --------------------------------------------------------------------- init

def run_init() -> None:
    log.debug("run_init: stage_root=%s config_file=%s", DEFAULT_STAGE_ROOT, CONFIG_FILE)
    DEFAULT_STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG)
        log.debug("wrote starter config: %s", CONFIG_FILE)
    else:
        log.debug("config already exists, left alone: %s", CONFIG_FILE)
    log.debug("stage dir ready: %s", DEFAULT_STAGE_ROOT)


# ---------------------------------------------------------------------- cli

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--init", is_flag=True, help="One-time local setup: create the stage directory and a starter config.")
@click.option("--dispatch", is_flag=True, hidden=True,
              help="Internal: invoked locally by `kitty @ launch`, never typed by hand.")
@click.option("--dest", default=None, metavar="DIR", help="Override destination directory.")
@click.option("--app", default=None, metavar="CMD", help="Override the app/command used to open the file.")
@click.option("--host", default=None, help="Override the remote host id (push mode only).")
@click.option("--no-open", "no_open", is_flag=True, help="Transfer only, don't open.")
@click.option("-L", "--log-level", default="warning", show_default=True,
              help="Logging level: debug, info, warning, error.")
@click.argument("paths", nargs=-1)
def main(init: bool, dispatch: bool, dest: str | None, app: str | None, host: str | None,
         no_open: bool, log_level: str, paths: tuple[str, ...]) -> None:
    """Push a remote file/dir to the local machine and open it there.

    The SAME script runs in two roles, on two machines:

    \b
      push (default) -- run in a remote shell:
        kitty-open somefile.png
        kitty-open ~/work/A/plots/          # a directory
      It transfers the path to a flat local staging area via `kitten
      transfer`, then asks the local kitty instance (over the forwarded
      remote-control socket) to re-invoke this same script in dispatch mode.

    \b
      dispatch (internal, --dispatch) -- invoked locally by `kitty @
      launch`, never typed by hand. Applies local MAPPINGS/DISPATCH config
      to decide the real destination directory and the app to open it with,
      then execs that app.

    \b
    One-time LOCAL setup (the machine you sit in front of):
    \b
      1. kitty.conf:
             allow_remote_control socket-only
             listen_on unix:/tmp/kitty-rc
             file_transfer_confirmation_bypass <password-or-file-per-kitty-docs>
    \b
      2. kitty's ssh.conf (or per-host block), so the RC socket gets
         forwarded over each ssh session using the `ssh` kitten:
             forward_remote_control yes
    \b
      3. Install this script at the same PATH-visible location on BOTH
         machines, e.g. ~/.local/bin/kitty-open (that's what makes "sync
         the one file between endpoints" work). LOCAL_SCRIPT in the source
         must match that path.
    \b
      4. Put the SAME bypass password/secret in
         ~/.config/kitty-open/transfer.pass (chmod 600) on both machines --
         consult `kitten transfer --help` and the kitty.conf docs for the
         exact accepted forms (literal password vs. file-path vs. fd
         number); untested here.
    \b
      5. Run `kitty-open --init` once, locally, to create the stage
         directory and a starter ~/.config/kitty-open/config.py.
    \b
      6. Edit ~/.config/kitty-open/config.py locally to add MAPPINGS /
         DISPATCH rules (e.g. route remote:~/work/A/* to ~/dropbox/A).

    \b
    Known rough edges to verify before relying on this (see inline notes):
      - whether kitty actually expands a leading "~/" in the `kitten
        transfer` destination against the LOCAL home directory (docs
        strongly imply yes via its differing-home-directory handling, but
        untested here).
      - the mkdir-before-transfer step for directories is fire-and-forget
        with a fixed sleep, not a true synchronous wait on the
        remote-control call.
    """
    setup_logging(log_level)

    if init:
        run_init()
        return

    if dispatch:
        stage_path, remote_host, remote_path = paths[:3]
        run_dispatch(stage_path, remote_host, remote_path, dest, app)
        return

    if not paths:
        raise click.UsageError("no file or directory given")

    run_push(list(paths), host, dest, app, no_open)


if __name__ == "__main__":
    main()
