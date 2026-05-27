!macro NSIS_HOOK_PREUNINSTALL
  MessageBox MB_YESNO|MB_ICONQUESTION "是否同时删除 Lumen 本地数据？这会删除 $APPDATA\com.lumen.desktop。" IDNO done
  RMDir /r "$APPDATA\com.lumen.desktop"
  RMDir /r "$LOCALAPPDATA\com.lumen.desktop"
done:
!macroend
