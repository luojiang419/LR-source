local LrTasks=import 'LrTasks'
local LrDialogs=import 'LrDialogs'
LrTasks.startAsyncTask(function()local ok,err=LrTasks.pcall(function()require('BridgeCore').undo()end);if not ok then LrDialogs.message('撤销失败',tostring(err),'warning')end end)
