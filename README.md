# TerminatorRemote

A Terminator plugin which adds features for ssh and docker/podman

## Clone Horizontally/Vertically

This will clone your current SSH/container session into a newly spawned terminal

Heavily inspired by https://github.com/ilgarm/terminator_plugins which is
no longer mainained

## Profile Host Matching

When you clone a remote session, you can apply a terminator profile based on host or container name

Inspired by https://github.com/GratefulTony/TerminatorHostWatch which does this via regex matching your PS1

## Installing
```shell
mkdir -p ~/.config/terminator/plugins
cp remote.py ~/.config/terminator/plugins/
```

Start Terminator. In Right Click -> Preferences -> Plugins, enable Remote

## Configuration

Plugin section in `~/.config/terminator/config` :
```
[plugins]
  [[Remote]]
    # Optional default profile for all SSH sessions
    ssh_default_profile = common_ssh_profile

    # Optional default profile for all container sessions
    container_default_profile = common_docker_profile

    # You can override above defaults by specifing a host with a profile key
    # ex:
    [[[foo]]]
      profile = foo_profile
```
