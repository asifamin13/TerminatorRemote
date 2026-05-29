# TerminatorRemote

A Terminator plugin which adds features for ssh and docker/podman to the context menu

![Alt Text](https://media.giphy.com/media/v1.Y2lkPTc5MGI3NjExejdidWNqYXh3dXc1bWNvcjJteXRkOTVsM24wNWQ0dzk4dnRydGJldSZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/fNB06vpKpYIDDewFFS/source.gif)

## Clone Horizontally/Vertically

This will clone your current SSH/container session into a newly spawned terminal.
By default, the CWD is inferred by regex-matching the PS1 in the terminal scrollback.

Heavily inspired by https://github.com/ilgarm/terminator_plugins which is
no longer mainained

## Clone into Highlighted Path

If you highlight a file path in the terminal before right-clicking, two additional
menu items will appear: **"Clone Horizontally into /the/path"** and
**"Clone Vertically into /the/path"**. These clone the remote session and `cd`
directly into the highlighted path — no regex or `pwd` detection needed.

This is useful when the CWD isn't in your prompt or you want to clone into a
different directory than the current one.

## Use pwd for CWD

When enabled via the context menu toggle, clone operations will send `pwd` to the
remote shell to determine the working directory instead of regex-matching the PS1.
This is more reliable when the CWD isn't visible in the prompt area (e.g. scrolled
off screen or a minimal PS1).

**Important:** This sends `pwd` to your remote shell, so the shell must be idle
(not running a long command). Uncheck the toggle if a command is running.

## SSH to Host

A submenu listing all hosts from `~/.ssh/config` (skipping wildcard patterns).
Clicking a host sends `ssh <host>` to the terminal. Supports `Include` directives
in your SSH config.

## Attach to Container

A submenu listing all running containers (via the Docker/Podman API). Each entry
shows the container name and image. Clicking a container sends
`podman exec -it <name> <shell>` to the terminal, using the `container_shell` config.

Only shown when the Docker/Podman API is available.

## Profile Host Matching

When you clone a remote session, you can apply a terminator profile based on host or container name

Inspired by https://github.com/GratefulTony/TerminatorHostWatch which does this via regex matching your PS1

## Docker/Podman API Integration

When the `docker` Python SDK is installed (`pip install docker`), the plugin
can use the Docker/Podman API for enhanced container support:

- **Container working directory**: Automatically detected via `docker inspect`,
  so `cd` is more reliable for containers
- **`--workdir` on clone**: Container clones use `docker exec -w /path` instead
  of sending a `cd` command after spawning

The plugin tries these API sockets in order:
1. `DOCKER_HOST` environment variable (if set)
2. Rootless Podman: `unix:///run/user/{uid}/podman/podman.sock`
3. Docker: `unix:///var/run/docker.sock`
4. System Podman: `unix:///run/podman/podman.sock`

If the SDK is not installed or no socket is available, the plugin falls back
to the existing psutil-based cmdline parsing — no functionality is lost.

**Podman users**: Enable the API socket with:
```shell
systemctl --user start podman.socket
```

## Installing
```shell
mkdir -p ~/.config/terminator/plugins
cp remote.py ~/.config/terminator/plugins/

# Optional: install Docker SDK for enhanced container support
pip install docker
```

Start Terminator. In Right Click -> Preferences -> Plugins, enable Remote

## Configuration

Plugin section in `~/.config/terminator/config` :
```toml
[plugins]
  [[Remote]]
    # Automatically clone when you split a terminal with a remote session
    auto_clone = False

    # When a terminal with a remote session is cloned, attempt to parse the
    # current working directory via the PS1 and 'cd' into it
    infer_cwd = True

    # Shell to use when cloning into a container (e.g. "bash --login", "zsh")
    container_shell = sh

    # Delay in seconds before sending cd command after clone (default 0.25)
    cd_delay = 0.25

    # Path to SSH config file (supports ~ expansion)
    ssh_config = ~/.ssh/config

    # Optional default profile for all SSH sessions
    ssh_default_profile = common_ssh_profile

    # Optional default profile for all container sessions
    container_default_profile = common_docker_profile

    # You can override above defaults by specifing a host with a profile key
    # and optionally send a command after connecting
    # ex:
    [[[foo]]]
      profile = foo_profile
      
    [[[sp-0]]]
      profile = sp_profile
      command = source ~/users/amin/bashrc
      command_delay = 1.0
      # Send command before cd (default True). Set to False to cd first.
      command_before_cd = True
```

## Debugging

To debug, start Terminator from another terminal emulator like so:

```shell
terminator -d --debug-classes Remote,SSHSession,ContainerSession,RemoteProcWatch,DockerAPI -u
```

## Development

Adding support for future types of "Remote Sessions" can be easily added by
subclassing `RemoteSession` and appending an instance to `Remote.remote_session_types`
