; Extra installer behaviour, included by electron-builder (nsis.include).
;
; Uninstalling removes the program and nothing else by default: the settings, the queue and the
; Twitch sign-in tokens live in %APPDATA%\TwitchRadio and survive, so a reinstall picks up where
; it left off. This asks once, defaulting to "No", whether to remove them as well. It is not asked
; during an in-place update or a silent uninstall, which never touch that folder.
!macro customUnInstall
  ${ifNot} ${isUpdated}
    ${ifNot} ${Silent}
      MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Also delete your Twitch Radio settings, song queue and Twitch sign-in tokens?$\r$\n$\r$\nChoose No to keep them in case you install Twitch Radio again." /SD IDNO IDNO twitchRadioKeepData
        SetShellVarContext current
        RMDir /r "$APPDATA\TwitchRadio"
      twitchRadioKeepData:
    ${endIf}
  ${endIf}
!macroend
