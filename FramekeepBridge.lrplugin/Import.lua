local LrTasks=import 'LrTasks'
local LrDialogs=import 'LrDialogs'
LrTasks.startAsyncTask(function()local ok,err=LrTasks.pcall(function()require('BridgeCore').importFile()end);if not ok then LrDialogs.message('清单导入失败',tostring(err),'warning')end end)
