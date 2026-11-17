# Android WebView 行滑动被子元素的隐式捕获取消

- 表现：从行内图片按钮开始横向滑动时，操作按钮闪现后收回；鼠标或键盘可能正常。
- 根因：触控 pointerdown 后浏览器将指针隐式捕获到子元素；横滑确认后调用行的 `setPointerCapture` 会先向原子元素发送 `lostpointercapture`，事件冒泡到列表。若无条件取消手势，会把正常的捕获转移误判为中断。
- 解决：仅当 `lostpointercapture` 的 `event.target` 等于当前捕获行时才取消；保留 `pointercancel`，pointerup 先清空手势再释放捕获。行及图片按钮设置 `touch-action:pan-y pinch-zoom`，允许纵向滚动。
- 验证：`安卓出入库/tests/test_record_trash.py` 使用 CDP 真触摸事件覆盖从图片滑动、左右展开、纵向滚动和取消；Android 14 / WebView 113 模拟器的真实页面验证删除、重启与滑动还原通过。
