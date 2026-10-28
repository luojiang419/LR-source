@echo off
setlocal
chcp 65001 >nul
set "PYTHONPATH=%~dp0..\.runtime\modelscope-publish-sdk;%PYTHONPATH%"
echo 请使用有权发布 jiangjiang419/framekeep-updates 的魔搭账号访问令牌登录。
echo Token 输入将隐藏，不需要在命令行参数或聊天中粘贴。
echo 令牌页面：https://modelscope.cn/my/myaccesstoken
python -X utf8 -m modelscope_hub.cli.main login
set "MODELSCOPE_LOGIN_EXIT=%ERRORLEVEL%"
if not "%MODELSCOPE_LOGIN_EXIT%"=="0" goto finish
python -X utf8 -m modelscope_hub.cli.main whoami
:finish
pause
exit /b %MODELSCOPE_LOGIN_EXIT%
