## Installation

### Windows

Download **Twitch Radio Setup** below and run it. It installs for your user only and keeps your settings in `%APPDATA%\TwitchRadio`.

**Windows SmartScreen:** the installer may show "Windows protected your PC" (see the signing note at the end). Choose **More info**, then **Run anyway**. To check the file first, compare its SHA-256 with `SHA256SUMS.txt` (`Get-FileHash <file>` in PowerShell). GitHub renames spaces in file names to dots, so the downloaded file is `Twitch.Radio.Setup.x.y.z.exe`.

### Linux (x86-64; built for Arch and CachyOS, should run on other recent distributions)

Download `Twitch-Radio-x.y.z-linux-x64.AppImage`, then:

```
chmod +x Twitch-Radio-*-linux-x64.AppImage
./Twitch-Radio-*-linux-x64.AppImage
```

The AppImage needs FUSE 2 (`sudo pacman -S fuse2`). Without it, extract the `.tar.gz` and run `./twitch-radio` inside. ffmpeg and Deno are bundled. Settings live in `~/.config/TwitchRadio`. Check downloads with `sha256sum -c --ignore-missing SHA256SUMS.txt`.
