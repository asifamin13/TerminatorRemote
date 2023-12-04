"""
Support for remote sessions like ssh, docker, podman, virsh

This plugin will look for a child remote session inside terminal using the
psutil API and provide mechanisms to clone session into a new terminal.
"""

import os
import time
import getopt
import argparse
import psutil

from typing import Optional, List

from gi.repository import Gtk, GLib

from terminatorlib.plugin import MenuItem
from terminatorlib.config import Config
from terminatorlib.terminator import Terminator
from terminatorlib.util import err, dbg

# Every plugin you want Terminator to load *must* be listed in 'AVAILABLE'
AVAILABLE = ['Remote']

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
    def __init__(self, exe='ssh'):
        """ constructor """
        RemoteSession.__init__(self, exe)

    def IsType(self, proc):
        """ check if this is an ssh session """
        return self.matches_by_name(proc)

    def GetHost(self, proc):
        """
        extract host from ssh command line
        """
        def extractHost(target):
            if '@' in target:
                return target.split('@')[1]
            return target
        # https://github.com/openssh/openssh-portable/blob/99a2df5e1994cdcb44ba2187b5f34d0e9190be91/ssh.c#L713
        # while ((opt = getopt(ac, av, "1246ab:c:e:fgi:kl:m:no:p:qstvx"
        #     "AB:CD:E:F:GI:J:KL:MNO:P:Q:R:S:TVw:W:XYy")) != -1) { /* HUZdhjruz */
        shortOpts = (
            "1246ab:c:e:fgi:kl:m:no:p:qstvx"
            "AB:CD:E:F:GI:J:KL:MNO:P:Q:R:S:TVw:W:XYy"
        )

        try:
            ssh_args = proc.cmdline()[1:]
            _, args = getopt.getopt(ssh_args, shortOpts)
            return extractHost(args[0])
        except Exception as e:
            err(f"caught error: {e}")

        return None

    def Clone(self, proc):
        """ ssh just needs to copy the cmdline """
        return proc.cmdline()

class ContainerSession(RemoteSession):
    """ container type sessions """
    def __init__(self, exe):
        """ constructor """
        super().__init__(exe)

    def IsType(self, proc):
        """ check if this is a running docker session """
        if not self.matches_by_name(proc):
            return False
        dbg(f"checking cmdline: {proc.cmdline()}")
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
        except Exception as e:
            err(f"caught exception {e}")
        return None

    def Clone(self, proc):
        """ get cmd to launch terminal into container session """
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
            return [ self.exe, 'exec', '-it', host, 'sh' ]

    def _get_command(self, proc):
        """ get type of container command, we only support interactive ones """
        interactiveCmds = { 'run', 'exec', 'attach' }
        for arg in proc.cmdline():
            if arg in interactiveCmds:
                return arg
        return None

    def _get_host_run(self, proc):
        """
        docker run, just check for --name
        If we dont have name (it would be random), give up.
        I'd have to parse docker ps / inspect or use the
        API which is a bit beyond the scope of this
        """
        try:
            idxOfName = proc.cmdline().index("--name")
            name = proc.cmdline()[idxOfName + 1]
            dbg(f"parsed container name: {name}")
            return name
        except Exception as e:
            err("caught error '{e}'")
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
        args, unknown = parser.parse_known_args(fullArgs[startIndex:])
        dbg(f"got args: {args}, unknown: {unknown}")
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
        parser = argparse.ArgumentParser()
        parser.add_argument("container")
        parser.add_argument('--detach-keys')
        parser.add_argument('-l', '--latest', action='store_true')
        parser.add_argument('--no-stdin', action='store_false')
        parser.add_argument('--sig-proxy', action='store_true')
        args, unknown = parser.parse_known_args(fullArgs[startIndex:])
        dbg(f"got args: {args}, unknown: {unknown}")
        return args.container

class Remote(MenuItem):
    """
    Add remote commands to the terminal menu
    """
    capabilities = ['terminal_menu']

    remote_session_types = [
        SSHSession(),
        ContainerSession('docker'),
        ContainerSession('podman')
    ]

    def __init__(self):
        """ constructor """
        MenuItem.__init__(self)
        self.config = self._get_config()
        dbg(f"using config: {self.config}")

        self.terminator = Terminator()
        self.timeout_id = None
        self.peers = set()
        self.remote_proc = None
        self.remote_type = None

    def _get_config(self):
        """ return configuration dict, ensure we have proper keys """
        config = {
            'ssh_default_profile': "",
            'container_default_profile': ""
        }
        user_config = Config().plugin_get_config(self.__class__.__name__)
        if user_config:
            config.update(user_config)
        return config

    def callback(self, menuitems, menu, terminal):
        """ Add our menu items to the menu """
        ret = self._has_remote_session(terminal.pid)
        if not ret:
            return
        child, remote_session = ret
        dbg(f"Found remote session {child}")

        item = Gtk.MenuItem.new_with_mnemonic('Clone Horizontally')
        item.connect(
            'activate',
            self._menu_item_activated,
            ('split-horiz', terminal)
        )
        menuitems.append(item)

        item = Gtk.MenuItem.new_with_mnemonic('Clone Vertically')
        item.connect(
            'activate',
            self._menu_item_activated,
            ('split-vert', terminal)
        )
        menuitems.append(item)

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
        remote_cmd = self.remote_type.Clone(self.remote_proc)
        spawn_cmd = " ".join(remote_cmd) # get as full string, not list of strings
        cmd = f"{spawn_cmd}{os.linesep}" # make sure we press "enter"
        dbg(f"will launch '{cmd}' into new terminal")
        vte = terminal.get_vte()
        vte.feed_child(cmd.encode())
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

    def _apply_host_settings(self, terminal):
        """ setup terminal if host is in config """
        profile = self._get_default_profile(self.remote_type)
        if not profile:
            dbg("no default profile specified in config")
            return
        # check host entry in config
        remoteHost = self.remote_type.GetHost(self.remote_proc)
        if not remoteHost:
            err(f"cannot determine host for proc {self.remote_proc}")
        elif remoteHost not in self.config:
            dbg(f"no host entry for {remoteHost}")
        else:
            hostSettings = self.config[remoteHost]
            if 'profile' in hostSettings:
                profile = hostSettings['profile']
            else:
                dbg(f"no profile entry for {remoteHost}")
        dbg(f"applying profile: {profile}")
        terminal.set_profile(None, profile=profile)

    def _has_remote_session(self, pid):
        """ check if this PID has a direct child with remote session """
        children = psutil.Process(pid).children(recursive=True)
        dbg(f"terminal PID {pid} has children: {children}")
        for child in children:
            with child.oneshot():
                for remote_session in self.remote_session_types:
                    if remote_session.IsType(child):
                        return (child, remote_session)
        return None

    def _menu_item_activated(self, _, args):
        """
        callback, args: ( signal, terminal )
        """
        signal, terminal = args

        ret = self._has_remote_session(terminal.pid)
        if not ret:
            err("lost remote session seen on context menu?")
            return
        child, remoteType = ret
        if not self.timeout_id: # check if we are already waiting
            self.remote_proc = child
            self.remote_type = remoteType
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
        else:
            err("already waiting for a terminal?")
