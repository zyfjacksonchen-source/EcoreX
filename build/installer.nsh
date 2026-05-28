!include LogicLib.nsh
!include nsDialogs.nsh

!ifndef BUILD_UNINSTALLER
Var EcoreXDesktopShortcutCheckbox
Var EcoreXStartupShortcutCheckbox
Var EcoreXCreateDesktopShortcut
Var EcoreXCreateStartupShortcut

!macro customPageAfterChangeDir
  Page custom EcoreXOptionsPageCreate EcoreXOptionsPageLeave
!macroend

Function EcoreXOptionsPageCreate
  nsDialogs::Create 1018
  Pop $0
  ${If} $0 == error
    Abort
  ${EndIf}

  ${NSD_CreateLabel} 0 0 100% 18u "安装选项"
  Pop $0
  ${NSD_CreateCheckbox} 0 28u 100% 12u "创建桌面快捷方式"
  Pop $EcoreXDesktopShortcutCheckbox
  ${NSD_Check} $EcoreXDesktopShortcutCheckbox
  ${NSD_CreateCheckbox} 0 50u 100% 12u "开机自动启动 EcoreX Agent"
  Pop $EcoreXStartupShortcutCheckbox

  nsDialogs::Show
FunctionEnd

Function EcoreXOptionsPageLeave
  ${NSD_GetState} $EcoreXDesktopShortcutCheckbox $EcoreXCreateDesktopShortcut
  ${NSD_GetState} $EcoreXStartupShortcutCheckbox $EcoreXCreateStartupShortcut
FunctionEnd

!macro customInstall
  StrCpy $0 "/currentuser"
  StrCmp "$installMode" "all" 0 +2
  StrCpy $0 "/allusers"

  ${If} $EcoreXCreateDesktopShortcut == ${BST_CHECKED}
    CreateShortCut "$DESKTOP\${SHORTCUT_NAME}.lnk" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" 0 "" "" "${APP_DESCRIPTION}"
  ${EndIf}

  ${If} $EcoreXCreateStartupShortcut == ${BST_CHECKED}
    CreateShortCut "$SMSTARTUP\${SHORTCUT_NAME}.lnk" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" 0 "" "" "${APP_DESCRIPTION}"
  ${EndIf}

  !ifdef MENU_FILENAME
    CreateDirectory "$SMPROGRAMS\${MENU_FILENAME}"
    CreateShortCut "$SMPROGRAMS\${MENU_FILENAME}\Uninstall ${SHORTCUT_NAME}.lnk" "$INSTDIR\${UNINSTALL_FILENAME}" "$0" "$INSTDIR\${UNINSTALL_FILENAME}" 0
  !else
    CreateShortCut "$SMPROGRAMS\Uninstall ${SHORTCUT_NAME}.lnk" "$INSTDIR\${UNINSTALL_FILENAME}" "$0" "$INSTDIR\${UNINSTALL_FILENAME}" 0
  !endif
!macroend
!endif

!macro customUnInstall
  Delete "$DESKTOP\${SHORTCUT_NAME}.lnk"
  Delete "$SMSTARTUP\${SHORTCUT_NAME}.lnk"

  !ifdef MENU_FILENAME
    Delete "$SMPROGRAMS\${MENU_FILENAME}\Uninstall ${SHORTCUT_NAME}.lnk"
  !else
    Delete "$SMPROGRAMS\Uninstall ${SHORTCUT_NAME}.lnk"
  !endif
!macroend
