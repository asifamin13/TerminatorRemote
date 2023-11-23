"""
Support for remote sessions like ssh, docker, podman, virsh

This plugin will look for a child remote sessions inside terminal using the
psutil API and provide mechanisms to clone session into a new terminal. 
"""

import os
import time
import getopt
import psutil

import gi
from gi.repository import Gtk, GLib

import terminatorlib.plugin as plugin

from terminatorlib.terminator import Terminator
from terminatorlib.util import err, dbg

# Every plugin you want Terminator to load *must* be listed in 'AVAILABLE'
AVAILABLE = ['Remote']

class RemoteSession(object):
    """
    Base class representing a 'Remote Session'
    """
    def __init__(self, exe):
        """
        constructor, exe acts like our type
        """
        self.exe = exe

    def IsType(self, proc):
        """ check if psutil.Process matches this type of remote session """
        raise NotImplementedError()

    def GetHost(self, proc):
        """ get remote host target """
        raise NotImplementedError()

    def matches_by_name(self, proc):
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
        https://github.com/openssh/openssh-portable/blob/99a2df5e1994cdcb44ba2187b5f34d0e9190be91/ssh.c#L713
        """
        def extractHost(target):
            if '@' in target:
                return target.split('@')[1]
            return target

        shortOpts = (
            "1246ab:c:e:fgi:kl:m:no:p:qstvx"
            "AB:CD:E:F:GI:J:KL:MNO:P:Q:R:S:TVw:W:XYy"
        )
        
        try:
            ssh_args = proc.cmdline()[1:]
            opts, args = getopt.getopt(ssh_args, shortOpts)
            return extractHost(args[0])
        except Exception as e:
            err(f"caught error: {e}")
        
        return None

class Remote(plugin.MenuItem):
    """
    Add remote commands to the terminal menu
    """
    capabilities = ['terminal_menu']

    remote_session_types = [
        SSHSession()
    ]

    def __init__(self):
        """ constructor """
        plugin.MenuItem.__init__(self)

        self.terminator = Terminator()

        self.remote_cmd = None
        self.timeout_id = None
        self.peers = set()

    def callback(self, menuitems, menu, terminal):
        """ Add our menu items to the menu """
        ret = self._has_remote_session(terminal.pid)
        if not ret:
            return
        child, remote_session = ret
        err(f"Found remote session {child} for host '{remote_session.GetHost(child)}'")

        item = Gtk.MenuItem.new_with_mnemonic('Clone Horizontally')
        item.connect('activate', self._menu_item_activated, (True, terminal))
        menuitems.append(item)

        item = Gtk.MenuItem.new_with_mnemonic('Clone Vertically')
        item.connect('activate', self._menu_item_activated, (False, terminal))
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
        spawn_cmd = " ".join(self.remote_cmd)
        cmd = f"{spawn_cmd}{os.linesep}"
        dbg(f"will launch '{cmd}' into new terminal")
        vte = terminal.get_vte()
        vte.feed_child(cmd.encode())

    def _has_remote_session(self, pid):
        """ check if this PID has a direct child with remote session """
        children = psutil.Process(pid).children(recursive=True)
        err(f"terminal PID {pid} has children: {children}")
        for child in children:
            with child.oneshot():
                for remote_session in self.remote_session_types:
                    if remote_session.IsType(child):
                        return (child, remote_session)
        return None

    def _menu_item_activated(self, menuitem, args):
        """
        callback, args: ( isHoriz, terminal )
        """
        isHoriz, terminal = args

        ret = self._has_remote_session(terminal.pid)
        if not ret:
            return
        child, remote_type = ret

        fullArgs = child.cmdline()
        dbg(f"found remote session: '{fullArgs}'")
        self.remote_cmd = fullArgs

        if not self.timeout_id:
            self.peers = self._get_all_terminals()
            dbg("First peer list: {}".format(self.peers))
            self.timeout_id = GLib.idle_add(
                self._poll_new_terminals,
                time.time()
            )
            signal = 'split-horiz' if isHoriz else 'split-vert'
            terminal.emit(signal, terminal.get_cwd())
        else:
            err("already have timer?!")
