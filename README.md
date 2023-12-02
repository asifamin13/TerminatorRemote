# TerminatorRemote

A Terminator plugin which adds features for ssh and docker/podman

## Clone Horizontally/Vertically

This will clone your current SSH/container session into a newly spawned terminal

Heavily inspired by https://github.com/ilgarm/terminator_plugins which is 
no longer mainained

## Profile Host Matching

When you clone a remote session, you can apply a terminator profile based on host or container name 

Inspired by https://github.com/ilgarm/terminator_plugins which does this via regex matching your PS1

## Installing
```shell
mkdir -p ~/.config/terminator/plugins
cp remote.py ~/.config/terminator/plugins/
```

Start Terminator. In Right Click -> Preferences -> Plugins, enable Remote
