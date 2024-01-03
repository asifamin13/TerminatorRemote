# TerminatorRemote

A Terminator plugin which adds features for ssh and docker/podman to the context menu

![Alt Text](https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExejdidWNqYXh3dXc1bWNvcjJteXRkOTVsM24wNWQ0dzk4dnRydGJldSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/fNB06vpKpYIDDewFFS/source.gif)

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
    # Automatically clone when you split a terminal with a remote session
    auto_clone = False

    # When a terminal with a remote session is cloned, attempt to parse the
    # current working directory via the PS1 and 'cd' into it
    infer_cwd = True

    # Optional default profile for all SSH sessions
    ssh_default_profile = common_ssh_profile

    # Optional default profile for all container sessions
    container_default_profile = common_docker_profile

    # You can override above defaults by specifing a host with a profile key
    # ex:
    [[[foo]]]
      profile = foo_profile
```

## Debugging

To debug, start Terminator from another terminal emulator like so:

```shell
terminator -d --debug-classes Remote,SSHSession,ContainerSession
```

## Development

Adding support for future types of "Remote Sessions" can be easily added by
subclassing `RemoteSession` and appending an instance to `Remote.remote_session_types`
