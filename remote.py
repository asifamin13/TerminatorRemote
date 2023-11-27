"""
Support for remote sessions like ssh, docker, podman, virsh

This plugin will look for a child remote session inside terminal using the
psutil API and provide mechanisms to clone session into a new terminal.
"""

import os
import time
import getopt
import psutil

from typing import Optional

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

    def matches_by_name(self, proc: psutil.Process) -> bool:
        """ https://psutil.readthedocs.io/en/latest/#find-process-by-name """
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

class Remote(MenuItem):
    """
    Add remote commands to the terminal menu
    """
    capabilities = ['terminal_menu']

    remote_session_types = [
        SSHSession()
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
            'ssh_default_profile': 'default'
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
        remote_cmd = self.remote_proc.cmdline()
        spawn_cmd = " ".join(remote_cmd)
        cmd = f"{spawn_cmd}{os.linesep}"
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
        return 'default'

    def _apply_host_settings(self, terminal):
        """ setup terminal if host is in config """
        profile = self._get_default_profile(self.remote_type)
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
