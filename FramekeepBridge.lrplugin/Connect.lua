local LrTasks=import 'LrTasks'
local LrDialogs=import 'LrDialogs'
LrTasks.startAsyncTask(function()local ok,err=LrTasks.pcall(function()require('BridgeCore').connect(false)end);if not ok then LrDialogs.message('末帧桥接错误',tostring(err),'warning')end end)
