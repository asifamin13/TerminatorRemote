"""
NAME
    remote.py - Add support for remote sessions like ssh and docker/podman

DESCRIPTION
    This plugin will look for a child remote session inside terminal using the
    psutil API and provide mechanisms in the context menu to clone session into
    a new terminal and/or change profiles based on the remote type or host.

    Cloning sessions inspired from https://github.com/ilgarm/terminator_plugins
      * Not maintained anymore

    Host profile matching inspired from https://github.com/GratefulTony/TerminatorHostWatch
      * This finds hosts by parsing the PS1 using a regex

INSTALLATION
    Put this file in ~/.config/terminator/plugins/

CONFIGURATION
    Plugin section in ~/.config/terminator/config
    [plugins]
      [[Remote]]

    Configuration keys:
      * auto_clone: Clone automatically when you split a remote session
      * infer_cwd: When a session is cloned, attempt to `cd` into working directory
      * ssh_default_profile: optional profile to apply to all SSH sessions
      * container_default_profile: optional profile to apply to all container sessions
      * ssh_command: SSH executable to use (default "ssh")
      * container_command: Container runtime executable to use (default "docker")
      * socket_path: Optional Docker/Podman API socket path (default: auto-detect)

    Host section:
      You can add host sections with a 'profile' key which will override the defaults

    ex)

    [plugins]
      [[Remote]]
        ssh_default_profile = common_ssh_profile
        container_default_profile = common_docker_profile
        auto_clone = False
        [[[foo]]]
          profile = foo_profile
        [[[bar]]]
          profile = bar_profile

DEBUGGING
    To debug, start Terminator from another terminal emulator like so:

    $ terminator -d --debug-classes Remote,SSHSession,ContainerSession,RemoteProcWatch -u

DEVELOPMENT
    support for future types of "Remote Sessions" can be easily added by
    subclassing `RemoteSession` and appending an instance to `Remote.remote_session_types`

AUTHORS
    The plugin was developed by Asif Amin <asifamin@utexas.edu>
"""

import os
import time
import glob
import getopt
import argparse
import re
import json
import socket
import http.client
import psutil
import asyncio
import threading
from typing import Optional, List

import gi
from gi.repository import Gtk, GLib, Gdk
gi.require_version('Vte', '2.91')
from gi.repository import Vte

from terminatorlib.plugin import MenuItem
from terminatorlib.config import Config
from terminatorlib.terminator import Terminator
from terminatorlib.util import err, dbg
from terminatorlib.translation import _

from terminatorlib.version import APP_NAME, APP_VERSION

AVAILABLE = ['Remote']

CD_CMD = "cd -- {cwd} 2>/dev/null"

# Cache VTE version check at module load instead of per-call
_VTE_VERSION = "{}.{}".format(
    Vte.get_major_version(), 
    Vte.get_minor_version()
)

def vte_get_text(vte_term, start_row, start_col, end_row, end_col):
    """ wrapper for get_text_range* based on Vte version """
    if _VTE_VERSION < "0.72":
        return vte_term.get_text_range(
            start_row=start_row,
            start_col=start_col,
            end_row=end_row,
            end_col=end_col, 
            is_selected=None, 
        )[0]
    return vte_term.get_text_range_format(
        format=Vte.Format.TEXT,
        start_row=start_row,
        start_col=start_col,
        end_row=end_row,
        end_col=end_col
    )[0]

class UnixHTTPConnection(http.client.HTTPConnection):
    """ http.client connection over a unix domain socket """
    def __init__(self, socket_path, timeout=2):
        super().__init__('localhost', timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


class DockerAPI(object):
    """
    Minimal Docker/Podman REST API client over a unix socket using only
    the standard library — no docker SDK dependency. Works with both
    Docker and Podman (Podman exposes a Docker-compatible API socket).
    Falls back gracefully when no API socket is accessible.
    """
    _instance = None

    @classmethod
    def get_instance(cls):
        """Get the singleton DockerAPI instance"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._socket = None  # path of the working socket
        self._tried_connect = False
        self.socket_path = None

    @staticmethod
    def _candidate_sockets(configured):
        """ ordered list of socket paths to try """
        uid = os.getuid()
        candidates = []

        # If a specific socket path is configured, try it first
        if configured:
            expanded = os.path.expanduser(configured)
            if expanded.startswith('unix://'):
                expanded = expanded[len('unix://'):]
            candidates.append(expanded)

        # Respect DOCKER_HOST env var if set (unix sockets only)
        docker_host = os.environ.get('DOCKER_HOST', '')
        if docker_host.startswith('unix://'):
            candidates.append(docker_host[len('unix://'):])
        elif docker_host:
            dbg(f"DOCKER_HOST '{docker_host}' is not a unix socket, skipping")

        # Rootless Podman (most common for personal workstations)
        candidates.append(f"/run/user/{uid}/podman/podman.sock")

        # Docker socket
        candidates.append("/var/run/docker.sock")

        # System Podman socket
        candidates.append("/run/podman/podman.sock")

        return candidates

    def _request(self, path):
        """ GET `path` from the API socket, return (status, body) or None """
        if not self._socket:
            return None
        conn = UnixHTTPConnection(self._socket)
        try:
            conn.request('GET', path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        except Exception as e:
            dbg(f"API request {path} failed: {e}")
            return None
        finally:
            conn.close()

    def _get_json(self, path):
        """ GET `path` and parse the JSON body, or None on any failure """
        ret = self._request(path)
        if not ret:
            return None
        status, body = ret
        if status != 200:
            dbg(f"API request {path} returned status {status}")
            return None
        try:
            return json.loads(body)
        except ValueError as e:
            dbg(f"bad JSON from {path}: {e}")
            return None

    def _connect(self):
        """
        Find a working Docker/Podman API socket.
        Returns self when an API is available, else None (so callers can
        keep the `if not api._connect()` guard pattern).
        """
        if self._tried_connect:
            return self if self._socket else None
        self._tried_connect = True

        for sock_path in self._candidate_sockets(self.socket_path):
            # check file existence first to avoid connection timeouts
            if not os.path.exists(sock_path):
                dbg(f"Socket file does not exist: {sock_path}")
                continue
            self._socket = sock_path
            ret = self._request('/_ping')
            if ret and ret[0] == 200:
                dbg(f"Connected to container API at {sock_path}")
                return self
            self._socket = None

        dbg("No container API socket available")
        return None

    def list_containers(self):
        """
        List running containers via GET /containers/json.
        Returns list of dicts with 'name', 'image', 'created' keys
        ('created' is a unix timestamp int, suitable for sorting).
        """
        data = self._get_json('/containers/json')
        if data is None:
            return []
        containers = []
        for c in data:
            names = c.get('Names') or []
            name = names[0].lstrip('/') if names else c.get('Id', '')[:12]
            containers.append({
                'name': name,
                'image': c.get('Image', ''),
                'created': c.get('Created', 0),
            })
        return containers

    def get_container_info(self, name_or_id):
        """
        Inspect a container via GET /containers/{name}/json.
        Returns dict with 'name', 'working_dir' keys, or None if not found.
        """
        attrs = self._get_json(f'/containers/{name_or_id}/json')
        if attrs is None:
            return None
        info = {
            'name': attrs.get('Name', '').lstrip('/'),
            'working_dir': attrs.get('Config', {}).get('WorkingDir', ''),
        }
        dbg(f"Got container info via API: {info}")
        return info


class RemoteSession(object):
    """
    API representing a 'Remote Session'
    """
    def __init__(self, exe):
        """
        constructor, exe acts like our type
        """
        self.exe = exe

    def IsType(self, proc: psutil.Process) -> bool:
        """ check if psutil.Process matches this type of remote session """
        raise NotImplementedError()

    def GetHost(self, proc: psutil.Process) -> Optional[str]:
        """ get remote host target """
        raise NotImplementedError()

    def Clone(self, proc: psutil.Process) -> List[str]:
        """ get the command to clone session """
        raise NotImplementedError()

    def matches_by_name(self, proc: psutil.Process) -> bool:
        """
        generic check if proc matches self.exe
        https://psutil.readthedocs.io/en/latest/#find-process-by-name
        """
        if self.exe == proc.name():
            return True
        if proc.exe():
            if self.exe == os.path.basename(proc.exe()):
                return True
        if proc.cmdline():
            if self.exe == proc.cmdline()[0]:
                return True
        return False

class SSHSession(RemoteSession):
    """ SSH sessions """
    # executables that spawn ssh as a non-interactive transport
    _transport_parents = {
        'rsync', 'scp', 'sftp', 'sftp-server', 'rsync-ssl',
        'git-remote-ssh', 'git-lfs', 'svn', 'unison'
    }
    # https://github.com/openssh/openssh-portable/blob/99a2df5e1994cdcb44ba2187b5f34d0e9190be91/ssh.c#L713
    # while ((opt = getopt(ac, av, "1246ab:c:e:fgi:kl:m:no:p:qstvx"
    #     "AB:CD:E:F:GI:J:KL:MNO:P:Q:R:S:TVw:W:XYy")) != -1) { /* HUZdhjruz */
    _ssh_short_opts = (
        "1246ab:c:e:fgi:kl:m:no:p:qstvx"
        "AB:CD:E:F:GI:J:KL:MNO:P:Q:R:S:TVw:W:XYy"
    )

    def __init__(self, exe='ssh'):
        """ constructor """
        RemoteSession.__init__(self, exe)

    @classmethod
    def _parse_ssh_args(cls, proc):
        """
        Parse ssh cmdline into (opts, args) using getopt.
        Returns (opts, args) or (None, None) on error.
        opts is a list of (option, value) tuples; args is the list of
        positional arguments (host, optional remote command, ...).
        """
        try:
            ssh_args = proc.cmdline()[1:]
            opts, args = getopt.getopt(ssh_args, cls._ssh_short_opts)
            return opts, args
        except psutil.NoSuchProcess:
            dbg("proc has gone away")
        except Exception as e:
            dbg(f"caught error parsing ssh args: {e}")
        return None, None

    def IsType(self, proc):
        """ check if this is an interactive ssh session """
        if not self.matches_by_name(proc):
            return False
        return not self._is_transport_ssh(proc)

    def _is_transport_ssh(self, proc):
        """
        Detect non-interactive ssh processes used as transport by
        rsync/scp/sftp/etc. so we don't treat them as interactive sessions.

        Signals that this ssh is transport (not a session to track):
          * parent process is a known file-transfer tool, OR
          * ssh was given a remote command (positional args after the host)
            without a -t/--force-tty flag
        """
        try:
            # Parent process check — rsync/scp/sftp all spawn ssh as a child
            parent = proc.parent()
            if parent is not None:
                pname = parent.name()
                if pname in self._transport_parents:
                    dbg(f"ssh proc {proc.pid} has transport parent '{pname}', skipping")
                    return True

            # Remote-command check: parse the ssh cmdline.
            # If there are positional args beyond the host AND no -t was requested,
            # this is a non-interactive one-shot (e.g. `ssh host "ls"`, rsync's
            # `ssh host rsync --server ...`).
            opts, args = self._parse_ssh_args(proc)
            if opts is None:
                # parse failed (proc gone or bad cmdline) — assume transport to
                # be safe and avoid injecting into something we can't understand
                return True
            has_tty = any(o == '-t' for o, _ in opts)
            if not has_tty and len(args) > 1:
                dbg(f"ssh proc {proc.pid} has remote command without -t, treating as transport")
                return True
        except psutil.NoSuchProcess:
            dbg("proc has gone away during transport check")
            return True
        except Exception as e:
            dbg(f"error during transport check, assuming not transport: {e}")
            return False
        return False

    def GetHost(self, proc):
        """
        extract host from ssh command line
        """
        def extractHost(target):
            if '@' in target:
                return target.split('@')[1]
            return target

        opts, args = self._parse_ssh_args(proc)
        if args:
            return extractHost(args[0])
        return None

    def Clone(self, proc):
        """ ssh just needs to copy the cmdline """
        return proc.cmdline()

class ContainerSession(RemoteSession):
    """ container type sessions """
    def __init__(self, exe):
        """ constructor """
        super().__init__(exe)
        # Pre-create ArgumentParser instances to avoid rebuilding on every call
        self._exec_parser = self._create_exec_parser()
        self._attach_parser = self._create_attach_parser()

    @staticmethod
    def _create_exec_parser():
        """ pre-create the exec argument parser """
        parser = argparse.ArgumentParser()
        parser.add_argument("container")
        parser.add_argument("command", nargs='?')
        parser.add_argument('-d', '--detach', action='store_true')
        parser.add_argument('--detach-keys')
        parser.add_argument('-e', '--env')
        parser.add_argument('--env-file')
        parser.add_argument('-i', '--interactive', action='store_true')
        parser.add_argument('-l', '--latest', action='store_true')
        parser.add_argument('--privileged')
        parser.add_argument('--preserve-fds')
        parser.add_argument('-t', '--tty', action='store_true')
        parser.add_argument('-u', '--user')
        parser.add_argument('-w', '--workdir')
        return parser

    @staticmethod
    def _create_attach_parser():
        """ pre-create the attach argument parser """
        parser = argparse.ArgumentParser()
        parser.add_argument("container")
        parser.add_argument('--detach-keys')
        parser.add_argument('-l', '--latest', action='store_true')
        parser.add_argument('--no-stdin', action='store_false')
        parser.add_argument('--sig-proxy', action='store_true')
        return parser

    def IsType(self, proc):
        """ check if this is a running docker session """
        if not self.matches_by_name(proc):
            return False
        # make sure this is an interactive run, exec, or attach
        return self._get_command(proc) != None

    def GetHost(self, proc):
        """ try to find container name from cmdline """
        # TODO: figure this out
        cmd = self._get_command(proc)
        if not cmd:
            return None
        try:
            if cmd == "run":
                return self._get_host_run(proc)
            elif cmd == "exec":
                return self._get_host_exec(proc)
            elif cmd == "attach":
                return self._get_host_attach(proc)
            err("unrecognized sub command?")
        except psutil.NoSuchProcess as e:
            dbg(f"proc has gone away: {e}")
        except Exception as e:
            err(f"caught exception {e}")
        return None

    def Clone(self, proc, shell=None):
        """ get cmd to launch terminal into container session """
        if shell is None:
            shell = 'sh'
        cmd = self._get_command(proc)
        if not cmd:
            err("shouldnt happen?")
            return proc.cmdline()
        if cmd in ["exec" , "attach"]:
            return proc.cmdline()
        # this is a docker run
        host = self.GetHost(proc)
        if not host:
            # we dont have host info
            # just make new container
            return proc.cmdline()
        else:
            # we should exec a terminal session here
            clone_cmd = [self.exe, 'exec', '-it', host] + shell.split()
            return clone_cmd

    def _get_command(self, proc):
        """ get type of container command, we only support interactive ones """
        interactiveCmds = { 'run', 'exec', 'attach' }
        try:
            for arg in proc.cmdline():
                if arg in interactiveCmds:
                    return arg
        except psutil.NoSuchProcess as e:
            dbg(f"process has gone away: {e}")
        except Exception as e:
            err(f"unhandled exception: {e}")
        return None

    def _get_host_run(self, proc):
        """
        docker/podman run — try to find the container name.
        1. Check for --name in cmdline
        2. Fall back to Docker/Podman API — pick the most recently
           created running container
        """
        # Try --name flag first
        try:
            idxOfName = proc.cmdline().index("--name")
            name = proc.cmdline()[idxOfName + 1]
            dbg(f"parsed container name: {name}")
            return name
        except ValueError:
            pass  # --name not in cmdline
        except Exception as e:
            dbg(f"error looking for --name: {e}")

        # Fall back to Docker/Podman API — pick most recently created container
        api = DockerAPI.get_instance()
        if not api._connect():
            dbg("No API available to find run container name")
            return None

        try:
            candidates = [
                (c['name'], c['created']) for c in api.list_containers()
            ]
            if candidates:
                # Sort by creation time, pick the most recently created
                candidates.sort(key=lambda x: x[1], reverse=True)
                name = candidates[0][0]
                dbg(f"Selected most recently created container: '{name}'")
                return name

        except Exception as e:
            dbg(f"Error finding container via API: {e}")

        dbg("Could not determine container name for run command")
        return None

    def _get_host_exec(self, proc):
        """
        get container name from docker exec cmdline
        FORMAT: podman exec [options] CONTAINER [COMMAND [ARG...]]
        Options:
            -d, --detach               Run the exec session in detached mode (backgrounded)
                --detach-keys string   Select the key sequence for detaching a container. Format is a single character [a-Z] or ctrl-<value> where <value> is one of: a-z, @, ^, [, , or _ (default "ctrl-p,ctrl-q")
            -e, --env stringArray      Set environment variables
                --env-file strings     Read in a file of environment variables
            -i, --interactive          Keep STDIN open even if not attached
            -l, --latest               Act on the latest container podman is aware of
                                        Not supported with the "--remote" flag
                --preserve-fds uint    Pass N additional file descriptors to the container
                --privileged           Give the process extended Linux capabilities inside the container.  The default is false
            -t, --tty                  Allocate a pseudo-TTY. The default is false
            -u, --user string          Sets the username or UID used and optionally the groupname or GID for the specified command
            -w, --workdir string       Working directory inside the container
        """
        fullArgs = proc.cmdline()
        startIndex = fullArgs.index('exec') + 1
        args, unknown = self._exec_parser.parse_known_args(fullArgs[startIndex:])
        # dbg(f"got args: {args}, unknown: {unknown}")
        return args.container

    def _get_host_attach(self, proc):
        """
        get container name from docker attach
        FORMAT: podman attach [options] container
        OPTIONS
            --detach-keys=sequence
                Specify the key sequence for detaching a container. Format is a single character [a-Z] or one or more ctrl-<value> characters where <value> is one of: a-z, @, ^, [, , or _.
                Specifying "" disables this feature. The default is ctrl-p,ctrl-q.

                This option can also be set in containers.conf(5) file.

            --latest, -l
                Instead  of  providing  the  container  name or ID, use the last created container.  Note: the last started container can be from other users of Podman on the host machine.
                (This option is not available with the remote Podman client, including Mac and Windows (excluding WSL2) machines)

            --no-stdin
                Do not attach STDIN. The default is false.

            --sig-proxy
                Proxy received signals to the container process (non-TTY mode only). SIGCHLD, SIGSTOP, and SIGKILL are not proxied.

                The default is true.
        """
        fullArgs = proc.cmdline()
        startIndex = fullArgs.index('attach') + 1
        args, unknown = self._attach_parser.parse_known_args(fullArgs[startIndex:])
        # dbg(f"got args: {args}, unknown: {unknown}")
        return args.container

class RemoteProcWatch(object):
    """
    cache current remote sessions
    """
    def __init__(self, session_types, poll_rate=0.5) -> None:
        """ constructor """
        self.remote_session_types = session_types
        self.poll_rate = poll_rate
        self.watches = dict() # pid -> None or (psutil.Process, RemoteSession)
        self.create_times = dict() # pid -> create_time (cached to avoid syscalls on UI thread)
        self._lock = threading.Lock()

        self.quit = False
        self.loop = None
        self.thread = None

    def _has_remote_session(self, pid):
        """ check if this PID has a direct child with remote session """
        # Try non-recursive first (cheap) — ssh/docker are typically direct children
        children = psutil.Process(pid).children(recursive=False)
        if not children:
            return None
        dbg(f"terminal PID {pid} has direct children: {children}!!")
        for child in children:
            with child.oneshot():
                for remote_session in self.remote_session_types:
                    if remote_session.IsType(child):
                        return (child, remote_session)
        # Fall back to recursive scan only if direct children had no match
        children = psutil.Process(pid).children(recursive=True)
        dbg(f"terminal PID {pid} has recursive children: {children}")
        for child in children:
            with child.oneshot():
                for remote_session in self.remote_session_types:
                    if remote_session.IsType(child):
                        return (child, remote_session)
        return None

    def Register(self, pid):
        """ watch PID for children """
        with self._lock:
            if pid in self.watches:
                return
            dbg(f"adding new pid {pid}")
            self.watches[pid] = None
            # cache create_time once to avoid repeated syscalls on the UI thread
            try:
                self.create_times[pid] = psutil.Process(pid).create_time()
            except psutil.NoSuchProcess:
                self.create_times[pid] = None
        self._ensure_thread()

    def _ensure_thread(self):
        """
        (re)start the poll thread if it isn't running.
        A threading.Thread object can only be started once, so if a
        previous poller exited (all watches were removed), build a
        fresh loop + thread instead of calling start() again.
        """
        if self.thread is not None and self.thread.is_alive():
            return
        self.quit = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._external_thread, daemon=True)
        self.thread.start()

    def GetPIDProcInfo(self, pid):
        """ get current remote proc info """
        with self._lock:
            if pid not in self.watches:
                return None
            return self.watches[pid]

    def GetCreateTime(self, pid):
        """ get cached create_time for pid, avoiding syscall on UI thread """
        with self._lock:
            return self.create_times.get(pid)

    async def _poll(self):
        """ check psutil proc info """
        while not self.quit:
            with self._lock:
                pids = list(self.watches.keys())
            for procPid in pids:
                try:
                    # Skip expensive re-scan if previously found child is still alive
                    with self._lock:
                        prev = self.watches.get(procPid)
                    if prev is not None:
                        child_proc, _ = prev
                        if child_proc.is_running():
                            continue
                    ret = self._has_remote_session(procPid)
                    with self._lock:
                        self.watches[procPid] = ret
                except psutil.NoSuchProcess as e:
                    dbg(f"removing proc: {procPid}")
                    with self._lock:
                        self.watches.pop(procPid, None)
                        self.create_times.pop(procPid, None)
                except Exception as e:
                    dbg(f"caught generic exception: {e}")
            with self._lock:
                if len(self.watches) == 0:
                    dbg(f"no watches, leaving!")
                    self.quit = True
                    break
            await asyncio.sleep(self.poll_rate)

    async def _async_main(self):
        """ async stuff """
        task = self.loop.create_task(self._poll())
        await task

    def _external_thread(self):
        """ external event loop """
        self.loop.run_until_complete(self._async_main())
        self.loop.close()

class Remote(MenuItem):
    """
    Add remote commands to the terminal menu

    NOTE: Terminator can instantiate this plugin multiple times (e.g. each
    time the terminal context menu is built). All long-lived state is kept
    on the class so every instance shares one RemoteProcWatch poller, one
    GLib watch timer, and one terminal->profile tracking dict.
    """
    capabilities = ['terminal_menu']

    remote_session_types = [
        SSHSession(),
        ContainerSession('docker'),
        ContainerSession('podman')
    ]

    # ---- shared (class-level) state, singletons across plugin instances ----
    # global plugin config
    config = None
    # single proc watch poller shared by all instances
    remote_proc_watch = None
    # current terminals with a remote session found via polling
    currRemoteTerminals = dict() # terminal -> last profile
    # terminals that have already received a host command (prevents double-sending
    # when the dropdown menu already scheduled one before the poller detects it)
    sent_host_commands = set()
    # single GLib watch timer shared by all instances
    watch_id = None

    # I hate using regex, got this from ChatGPT 3.5
    # This should try to match a sane linux file path that can
    # have alphanumeric characters, ~, underscores, hyphens, and dots
    cwd_regex = re.compile(
        r'(\/(?:[\w.-]+\/)*[\w.-]+|\~(?:\/[\w.-]+)*)+(?:\.\w+)?'
    )

    def __init__(self):
        """ constructor """
        MenuItem.__init__(self)
        dbg("Remote instance created")

        if not Remote.config:
            Remote.config = Remote.get_config()
            dbg(f"using config: {self.config}")

        self.terminator = Terminator()

        # current terminal instance data
        self.peers = set()
        self.remote_proc = None
        self.remote_type = None
        self.remote_cwd = None
        self.timeout_id = None

        # Proc watch poller — create exactly once
        if Remote.remote_proc_watch is None:
            Remote.remote_proc_watch = RemoteProcWatch(self.remote_session_types)

        # Watch timer + one-time API pre-connect — install exactly once
        if Remote.watch_id is None:
            Remote.watch_id = GLib.timeout_add(
                500,
                self._update_watches,
                None
            )

            # Pre-connect to Docker/Podman API at plugin load time
            # so the first right-click menu doesn't have a delay
            api = DockerAPI.get_instance()
            socket_path = self.config.get('socket_path', '')
            if socket_path:
                api.socket_path = socket_path
            api._connect()

    def _isNewlySpawned(self, pid):
        create_time = self.remote_proc_watch.GetCreateTime(pid)
        if create_time is None:
            return False
        return abs(time.time() - create_time) < 3

    def _update_watches(self, _):
        """
        Watch for new terminals in background
        """
        for terminal in self.terminator.terminals:
            self.remote_proc_watch.Register(terminal.pid)
            ret = self.remote_proc_watch.GetPIDProcInfo(terminal.pid)
            if ret:
                child, remoteType = ret
                if terminal not in self.currRemoteTerminals:
                    self._apply_host_settings(
                        terminal=terminal,
                        proc=child,
                        proc_type=remoteType
                    )
                    self._send_host_command(terminal, child, remoteType)
            else:
                if terminal in self.currRemoteTerminals and not self._isNewlySpawned(terminal.pid):
                    dbg(f"restoring original profile: {self.currRemoteTerminals[terminal]}")
                    terminal.set_profile(None, profile=self.currRemoteTerminals[terminal])
                    self.currRemoteTerminals.pop(terminal)
                    Remote.sent_host_commands.discard(terminal)
        return True

    @classmethod
    def get_config(cls):
        """ return configuration dict, ensure we have proper keys """
        config = {
            'ssh_default_profile': "",
            'container_default_profile': "",
            'auto_clone': "False",
            'infer_cwd': "True",
            'use_pwd': "False",
            'container_shell': "sh",
            'ssh_config': "~/.ssh/config",
            'cd_delay': "0.25",
            'ssh_command': "ssh",
            'container_command': "docker",
            'socket_path': ""
        }
        user_config = Config().plugin_get_config(cls.__name__)
        dbg(f"read user config: {user_config}")
        if user_config:
            config.update(user_config)

        def get_as_bool(config, key):
            try:
                config[key] = config[key].lower() == 'true'
            except Exception as e:
                err(f"problem parsing {key} as bool: {e}")
                config[key] = False

        get_as_bool(config, 'auto_clone')
        get_as_bool(config, 'infer_cwd')
        get_as_bool(config, 'use_pwd')
        return config

    def _get_cwd_from_lines(self, terminal, N=3):
        """
        get last N lines in terminal and try to infer the CWD
        by finding the last sane linux file path. This assumes there
        is a PS1 which outputs the working directory
        """
        vte = terminal.get_vte()
        currCol, currRow = vte.get_cursor_position()
        lines = vte_get_text(
            vte_term=vte,
            start_row=max(0, currRow - N),
            start_col=0,
            end_row=currRow,
            end_col=currCol
        )
        if lines:
            matches = list(self.cwd_regex.finditer(lines))
            if matches:
                lastMatch = matches[-1]
                dbg(f"Inferred remote cwd: {lastMatch.group()}")
                return lastMatch.group()
        dbg(f"cant find remote cwd in '{lines}'")
        return None

    def _get_cwd_via_pwd(self, terminal, callback):
        """
        Send 'pwd' to the terminal and parse the output to get the CWD.
        This is more reliable than regex-based detection but requires the
        shell to be idle. Calls callback(cwd_string_or_None) when done.
        """
        vte = terminal.get_vte()
        currCol, currRow = vte.get_cursor_position()

        # Send pwd command
        vte.feed_child(b'pwd\n')

        def read_pwd_output():
            newCol, newRow = vte.get_cursor_position()
            text = vte_get_text(
                vte_term=vte,
                start_row=currRow,
                start_col=currCol,
                end_row=newRow,
                end_col=newCol
            )
            cwd = None
            if text:
                dbg(f"pwd output text: '{text}'")
                for line in text.split('\n'):
                    line = line.strip()
                    # pwd outputs the absolute path followed by a newline
                    # take the first non-empty line
                    if line and line != 'pwd':
                        cwd = line
                        break
            if cwd:
                dbg(f"Got CWD via pwd: {cwd}")
            else:
                dbg(f"Could not parse pwd output from '{text}'")
            callback(cwd)
            return False  # run once

        GLib.timeout_add(500, read_pwd_output)

    def _get_selected_path(self, terminal):
        """
        Get the currently selected text from the terminal and check if it
        looks like a file path. Returns the path string or None.
        Uses the primary selection (highlight buffer), not the clipboard.
        When the user explicitly highlights text that starts with / or ~/,
        trust it verbatim rather than running it through the regex (which
        can mangle paths with underscores or other characters).
        """
        vte = terminal.get_vte()
        if not vte.get_has_selection():
            return None
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_PRIMARY)
        text = clipboard.wait_for_text()
        if not text:
            return None
        text = text.strip()
        # Trust user selection directly if it looks like an absolute or home path
        if text.startswith('/') or text.startswith('~/'):
            dbg(f"Using raw selection as path: {text}")
            return text
        # Fall back to regex for paths embedded in other text
        match = self.cwd_regex.search(text)
        if match:
            path = match.group()
            dbg(f"Found path in selection via regex: {path}")
            return path
        dbg(f"Selection does not look like a path: '{text}'")
        return None

    def _parse_ssh_config(self):
        """
        Parse ~/.ssh/config and return a list of host aliases.
        Skips wildcard patterns like '*' or '*.example.com'.
        Follows Include directives.
        """
        hosts = []
        seen = set()

        def parse_file(filepath):
            try:
                with open(os.path.expanduser(filepath), 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('#') or not line:
                            continue
                        parts = line.split()
                        if len(parts) >= 2:
                            if parts[0].lower() == 'host':
                                for host in parts[1:]:
                                    # Skip wildcards and negation patterns
                                    if '*' in host or '?' in host or host.startswith('!'):
                                        continue
                                    if host not in seen:
                                        seen.add(host)
                                        hosts.append(host)
                            elif parts[0].lower() == 'include':
                                # Follow Include directives
                                include_path = os.path.expanduser(parts[1])
                                for fpath in sorted(glob.glob(include_path)):
                                    parse_file(fpath)

            except FileNotFoundError:
                dbg(f"SSH config file not found: {filepath}")
            except Exception as e:
                dbg(f"Error parsing SSH config {filepath}: {e}")

        parse_file(self.config['ssh_config'])
        hosts.sort()
        return hosts

    def _get_running_containers(self):
        """
        Get list of running containers via Docker/Podman API.
        Returns list of (name, image) tuples, sorted by name.
        """
        api = DockerAPI.get_instance()
        if not api._connect():
            return []
        
        try:
            containers = [
                (c['name'], c['image']) for c in api.list_containers()
            ]
            containers.sort(key=lambda x: x[0])
            return containers
        except Exception as e:
            dbg(f"Error listing containers: {e}")
            return []

    def _send_delayed_command(self, vte, command, delay_ms):
        """Send a command to the terminal after a delay"""
        def send():
            cmd = f"{command}\n"
            dbg(f"Sending delayed command '{command}'")
            vte.feed_child(cmd.encode())
            return False  # run once
        GLib.timeout_add(delay_ms, send)

    def _send_host_command(self, terminal, child, remote_session):
        """
        Send the configured host command when a manually-started remote
        session is first detected by the poller. This covers the case where
        the user types `ssh foo` or `docker exec ...` directly instead of
        using the dropdown menu.

        Uses sent_host_commands to avoid double-sending when the dropdown
        menu (or clone) already scheduled the command before the poller
        detected the remote session.
        """
        if terminal in Remote.sent_host_commands:
            dbg(f"Host command already sent or scheduled for this terminal, skipping")
            return

        try:
            if child.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                dbg(f"Process {child.pid} is {child.status()}, skipping host command")
                return
        except psutil.NoSuchProcess:
            dbg(f"Process {child.pid} no longer exists, skipping host command")
            return

        remoteHost = remote_session.GetHost(child)
        if not remoteHost:
            dbg("cannot determine host for manually-started session, skipping command")
            return
        host_config = self.config.get(remoteHost, {})
        command = host_config.get('command', '')
        if not command:
            return

        delay = float(host_config.get('command_delay', 1.0))
        vte = terminal.get_vte()
        dbg(f"Manually-started session detected for '{remoteHost}', will send command '{command}' after {delay}s")
        Remote.sent_host_commands.add(terminal)
        self._send_delayed_command(vte, command, int(delay * 1000))

    def _ssh_to_host(self, terminal, host):
        """Send ssh command to terminal, optionally followed by a post-connect command"""
        vte = terminal.get_vte()
        ssh_exe = self.config['ssh_command']
        cmd = f"{ssh_exe} {host}\n"
        dbg(f"Sending '{cmd.strip()}' to terminal")
        vte.feed_child(cmd.encode())

        # Check host config for a post-connect command
        host_config = self.config.get(host, {})
        command = host_config.get('command', '')
        if command:
            delay = float(host_config.get('command_delay', 1.0))
            dbg(f"Will send command '{command}' after {delay}s (host config for '{host}')")
            self._send_delayed_command(vte, command, int(delay * 1000))
            # Mark as sent so the poller doesn't double-send when it detects the SSH process
            Remote.sent_host_commands.add(terminal)

    def _attach_to_container(self, terminal, name):
        """Send exec command to terminal using configured shell, optionally followed by a post-connect command"""
        vte = terminal.get_vte()
        shell = self.config['container_shell']
        container_exe = self.config['container_command']
        cmd = f"{container_exe} exec -it {name} {shell}\n"
        dbg(f"Sending '{cmd.strip()}' to terminal")
        vte.feed_child(cmd.encode())

        # Check container host config for a post-connect command
        host_config = self.config.get(name, {})
        command = host_config.get('command', '')
        if command:
            delay = float(host_config.get('command_delay', 1.0))
            dbg(f"Will send command '{command}' after {delay}s (host config for '{name}')")
            self._send_delayed_command(vte, command, int(delay * 1000))
            # Mark as sent so the poller doesn't double-send when it detects the container process
            Remote.sent_host_commands.add(terminal)

    def callback(self, menuitems, menu, terminal):
        """ Add our menu items to the menu """

        def get_image_menuitem(title, horiz):
            item = Gtk.ImageMenuItem.new_with_mnemonic(title)
            image = Gtk.Image()
            image.set_from_icon_name(
                "{}_{}".format(APP_NAME, "horiz" if horiz else "vert"),
                Gtk.IconSize.MENU
            )
            item.set_image(image)
            if hasattr(item, 'set_always_show_image'):
                item.set_always_show_image(True)
            return item

        # Check for existing remote session
        ret = self.remote_proc_watch.GetPIDProcInfo(terminal.pid)

        if not ret:
            # No remote session — show options to launch new sessions
            ssh_hosts = self._parse_ssh_config()
            containers = self._get_running_containers()

            if ssh_hosts or containers:
                menuitems.append(Gtk.SeparatorMenuItem())

            if ssh_hosts:
                ssh_menu = Gtk.Menu()
                ssh_item = Gtk.MenuItem(_('SSH to Host'))
                ssh_item.set_submenu(ssh_menu)
                for host in ssh_hosts:
                    host_item = Gtk.MenuItem(host)
                    host_item.connect('activate', lambda w, h=host: self._ssh_to_host(terminal, h))
                    ssh_menu.append(host_item)
                ssh_menu.show_all()
                menuitems.append(ssh_item)

            if containers:
                container_menu = Gtk.Menu()
                container_item = Gtk.MenuItem(_('Attach to Container'))
                container_item.set_submenu(container_menu)
                for name, image in containers:
                    label = f"{name} ({image})" if image else name
                    c_item = Gtk.MenuItem(label)
                    c_item.connect('activate', lambda w, n=name: self._attach_to_container(terminal, n))
                    container_menu.append(c_item)
                container_menu.show_all()
                menuitems.append(container_item)

            return

        child, remote_session = ret
        dbg(f"Found remote session {child}")

        # separator before clone commands
        menuitems.append(Gtk.SeparatorMenuItem())
        
        # if we have split-auto signal
        if APP_VERSION >= '2.1.3':
            item = Gtk.MenuItem.new_with_mnemonic(_('Clone Auto'))
            item.connect(
                'activate',
                self._menu_item_activated,
                ('split-auto', terminal)
            )
            menuitems.append(item)

        # normal split buttons
        item = get_image_menuitem(_('Clone Horizontally'), horiz=True)
        item.connect(
            'activate',
            self._menu_item_activated,
            ('split-horiz', terminal)
        )
        menuitems.append(item)

        item = get_image_menuitem(_('Clone Vertically'), horiz=False)
        item.connect(
            'activate',
            self._menu_item_activated,
            ('split-vert', terminal)
        )
        menuitems.append(item)

        # "Clone into <path>" items — only shown when a path is selected
        selected_path = self._get_selected_path(terminal)
        if selected_path:
            item = get_image_menuitem(
                _('Clone Horizontally into %s') % selected_path, horiz=True
            )
            item.connect(
                'activate',
                self._menu_item_activated_into,
                ('split-horiz', terminal, selected_path)
            )
            menuitems.append(item)

            item = get_image_menuitem(
                _('Clone Vertically into %s') % selected_path, horiz=False
            )
            item.connect(
                'activate',
                self._menu_item_activated_into,
                ('split-vert', terminal, selected_path)
            )
            menuitems.append(item)

        # toggle to use pwd for CWD detection instead of regex
        item = Gtk.CheckMenuItem(_('Use pwd for CWD'))
        item.set_active(self.config['use_pwd'])
        item.connect(
            'toggled',
            self._on_use_pwd,
            None
        )
        menuitems.append(item)

        # add option to clone on split
        item = Gtk.CheckMenuItem(_('Clone On Split'))
        item.set_active(self.config['auto_clone'])
        item.connect(
            'toggled',
            self._on_clone_on_split,
            None
        )
        menuitems.append(item)

        # find the split items and add our clone handlers when they finish
        if self.config['auto_clone']:
            self.peers = self._get_all_terminals()
            for child in menu.get_children():
                if 'Split' in child.get_label():
                    dbg(f"handling split on menu item '{child.get_label()}'")
                    child.connect_after(
                        'activate', self._split_axis, terminal
                    )

    def _on_clone_on_split(self, widget, data):
        """ handle check text box """
        self.config['auto_clone'] = widget.get_active()

    def _on_use_pwd(self, widget, data):
        """ handle use pwd toggle """
        self.config['use_pwd'] = widget.get_active()

    def _menu_item_activated_into(self, _, args):
        """
        clone callback with explicit CWD from selected text,
        args: ( signal, terminal, cwd_path )
        Bypasses regex/pwd detection — uses the highlighted path directly.
        """
        signal, terminal, cwd_path = args

        ret = self.remote_proc_watch.GetPIDProcInfo(terminal.pid)
        if not ret:
            err("lost remote session seen on context menu?")
            return
        child, remoteType = ret
        if not self.timeout_id:
            self.remote_proc = child
            self.remote_type = remoteType
            self._continue_clone(signal, terminal, cwd_path)
        else:
            err("already waiting for a terminal?")

    def _poll_new_terminals(self, start_time):
        """
        Watch for new terminals
        TODO: I'd rather have a signal for when the new terminal is spawned
        """
        currPeers = self._get_all_terminals()
        if len(currPeers) != len(self.peers):
            # parent container changed, get the added child
            newPeers = [ x for x in currPeers if x not in self.peers ]
            if not len(newPeers):
                err("container removed children?!")
                return False
            dbg(f"Container has new children: {newPeers}")
            if len(newPeers) != 1:
                err("container has more than one child?!")
            newTermUUID = newPeers[0]
            newTerminal = self.terminator.find_terminal_by_uuid(newTermUUID.urn)
            self._spawn_remote_session(newTerminal)
            self.timeout_id = None
            self.newPeers = None
            return False

        # check if we have been polling too long
        if abs(time.time() - start_time) > 0.1:
            err("timeout polling for terminals")
            self.timeout_id = None
            self.newPeers = None
            return False

        dbg("polling for new terminals...")
        return True

    def _get_all_terminals(self):
        """ get all unique terminal instances """
        peers = set()
        try:
            peers = { x.uuid for x in self.terminator.terminals }
        except Exception as e:
            err(f"caught exception getting terminals: {e}")
        return peers

    def _spawn_remote_session(self, terminal):
        """ spawn user session into terminal """
        if isinstance(self.remote_type, ContainerSession):
            remote_cmd = self.remote_type.Clone(
                self.remote_proc,
                shell=self.config['container_shell']
            )
        else:
            remote_cmd = self.remote_type.Clone(self.remote_proc)

        spawn_cmd = " ".join(remote_cmd) # get as full string, not list of strings
        cmd = f"{spawn_cmd}{os.linesep}" # make sure we press "enter"
        
        dbg(f"will launch '{cmd}' into new terminal")
        vte = terminal.get_vte()
        vte.feed_child(cmd.encode())

        # Check host config for a post-connect command
        remoteHost = self.remote_type.GetHost(self.remote_proc)
        host_command = None
        host_command_delay = 1.0
        command_before_cd = True
        if remoteHost and remoteHost in self.config:
            host_config = self.config[remoteHost]
            host_command = host_config.get('command', '')
            host_command_delay = float(host_config.get('command_delay', 1.0))
            command_before_cd = host_config.get('command_before_cd', 'true').lower() == 'true'

        has_cd = self.remote_cwd not in (None, "", "~")
        cd_delay_ms = int(float(self.config['cd_delay']) * 1000)
        command_delay_ms = int(host_command_delay * 1000)

        if not has_cd:
            # No cd to send — just schedule the post-connect command if any
            if host_command:
                dbg(f"Will send command '{host_command}' after {host_command_delay}s (host config for '{remoteHost}')")
                self._send_delayed_command(vte, host_command, command_delay_ms)
        elif command_before_cd and host_command:
            # Command first, then cd
            # 1. Wait command_delay → send command
            # 2. Wait cd_delay → send cd
            dbg(f"Will send command '{host_command}' after {host_command_delay}s, then cd after {cd_delay_ms}ms more (host config for '{remoteHost}')")
            def send_command_then_cd():
                vte.feed_child(f"{host_command}\n".encode())
                snippet = CD_CMD.format(cwd=self.remote_cwd) + os.linesep
                def send_cd():
                    dbg(f"Sending cd after command")
                    vte.feed_child(snippet.encode())
                    return False
                GLib.timeout_add(cd_delay_ms, send_cd)
                return False
            GLib.timeout_add(command_delay_ms, send_command_then_cd)
        elif host_command:
            # Cd first, then command
            # 1. Wait cd_delay → send cd
            # 2. Wait remaining command_delay → send command
            snippet = CD_CMD.format(cwd=self.remote_cwd) + os.linesep
            dbg(f"Will send cd after {cd_delay_ms}ms, then command '{host_command}' after {command_delay_ms}ms more (host config for '{remoteHost}')")
            def send_cd_then_command():
                vte.feed_child(snippet.encode())
                remaining_delay = max(0, command_delay_ms - cd_delay_ms)
                self._send_delayed_command(vte, host_command, remaining_delay)
                return False
            GLib.timeout_add(cd_delay_ms, send_cd_then_command)
        else:
            # Only cd, no command
            snippet = CD_CMD.format(cwd=self.remote_cwd) + os.linesep
            dbg(f"will send snippet '{snippet}' into new terminal after {cd_delay_ms}ms")
            def send_later():
                vte.feed_child(snippet.encode())
                return False
            GLib.timeout_add(cd_delay_ms, send_later)

        # Mark as sent so the poller doesn't double-send when it detects the cloned session
        if host_command:
            Remote.sent_host_commands.add(terminal)

        self._apply_host_settings(terminal)

    def _get_default_profile(self, remote_type):
        """
        get default profile from config
        maybe more useful in the future...
        """
        if isinstance(remote_type, SSHSession):
            return self.config['ssh_default_profile']
        if isinstance(remote_type, ContainerSession):
            return self.config['container_default_profile']
        return ''

    def _apply_host_settings(self, terminal, proc=None, proc_type=None):
        """ setup terminal if host is in config """
        remote_proc = self.remote_proc if proc is None else proc
        remote_type = self.remote_type if proc_type is None else proc_type

        # Guard: skip if process is terminated
        try:
            if remote_proc.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                dbg(f"Process {remote_proc.pid} is {remote_proc.status()}, skipping host settings")
                return
        except psutil.NoSuchProcess:
            dbg(f"Process {remote_proc.pid} no longer exists, skipping host settings")
            return

        profile = self._get_default_profile(remote_type)
        if not profile:
            dbg("no default profile specified in config")
        # check host entry in config
        remoteHost = remote_type.GetHost(remote_proc)
        if not remoteHost:
            dbg(f"cannot determine host for proc {remote_proc}")
        elif remoteHost not in self.config:
            # dbg(f"no host entry for {remoteHost}")
            pass
        else:
            hostSettings = self.config[remoteHost]
            if 'profile' in hostSettings:
                profile = hostSettings['profile']
            # else:
            #     dbg(f"no profile entry for {remoteHost}")
        if not profile:
            dbg("cant find a profile in config")
            # Still track this terminal so we don't re-process it every poll cycle
            if terminal not in self.currRemoteTerminals:
                self.currRemoteTerminals[terminal] = terminal.get_profile()
            return
        if terminal.get_profile() != profile:
            dbg(f"applying profile: {profile}")
            self.currRemoteTerminals[terminal] = terminal.get_profile()
            terminal.set_profile(None, profile=profile)

    def _split_axis(self, widget, terminal):
        """ handle upstream split command, called AFTER default handler """
        dbg(f"handling split on terminal {terminal}!")
        # make sure original terminal still has remote session
        ret = self.remote_proc_watch.GetPIDProcInfo(terminal.pid)
        if not ret:
            err("lost remote session seen on context menu?")
            return
        self.remote_proc, self.remote_type = ret
        if self.config['infer_cwd']:
            self.remote_cwd = self._get_cwd_from_lines(terminal)
        self._apply_host_settings(terminal)

        # original split command should have finished due to our
        # connect_after. Try to find the new terminal since we
        # last activated the context menu.
        currPeers = self._get_all_terminals()
        if len(currPeers) != len(self.peers):
            # parent container changed, get the added child
            newPeers = [ x for x in currPeers if x not in self.peers ]
            if not len(newPeers):
                err("container removed children?!")
                return False
            dbg(f"Container has new children: {newPeers}")
            if len(newPeers) != 1:
                err("container has more than one child?!")
            newTermUUID = newPeers[0]
            newTerminal = self.terminator.find_terminal_by_uuid(newTermUUID.urn)
            self._spawn_remote_session(newTerminal)
        else:
            err("cant figure out the new terminal?")

    def _continue_clone(self, signal, terminal, remote_cwd):
        """Continue the clone process after CWD has been determined"""
        self.remote_cwd = remote_cwd
        # get list of current terminals, we will watch for a new one
        self.peers = self._get_all_terminals()
        dbg("First peer list: {}".format(self.peers))
        # launch idle callback to poll for new terminals
        self.timeout_id = GLib.idle_add(
            self._poll_new_terminals,
            time.time()
        )
        self._apply_host_settings(terminal)
        # launch new terminal
        terminal.emit(signal, terminal.get_cwd())

    def _menu_item_activated(self, _, args):
        """
        clone callback, args: ( signal, terminal )
        """
        signal, terminal = args

        ret = self.remote_proc_watch.GetPIDProcInfo(terminal.pid)
        if not ret:
            err("lost remote session seen on context menu?")
            return
        child, remoteType = ret
        if not self.timeout_id: # check if we are already waiting
            self.remote_proc = child
            self.remote_type = remoteType
            if self.config['use_pwd']:
                # Use pwd for CWD detection (requires idle shell)
                self.timeout_id = True  # sentinel to prevent re-entry
                self._get_cwd_via_pwd(
                    terminal,
                    lambda cwd: self._continue_clone(signal, terminal, cwd)
                )
            elif self.config['infer_cwd']:
                remote_cwd = self._get_cwd_from_lines(terminal)
                self._continue_clone(signal, terminal, remote_cwd)
            else:
                self._continue_clone(signal, terminal, None)
        else:
            err("already waiting for a terminal?")