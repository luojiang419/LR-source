# 浏览器组合输入事件需使用 addEventListener

## 表现与根因

中文输入法尚未完成选字就触发搜索，或等待组合输入结束的自动查询一直不执行。

在本机 Chrome 实测，`'oncompositionstart' in document.createElement('input')` 返回 `false`；给输入框赋值 `oncompositionstart`、`oncompositionend` 只是增加普通对象属性，派发 `CompositionEvent` 不会调用它们。不能按 `oninput` 的写法假定所有事件都支持 DOM 属性处理器。

## 解决方法

用 `addEventListener('compositionstart', ...)` 和 `addEventListener('compositionend', ...)` 监听。组合开始取消防抖并设置标记；`input` 同时检查内部组合标记和 `event.isComposing`，组合结束后重新启动防抖。手动提交也检查组合标记，避免确认候选词的 Enter 被当成搜索提交。

## 验证

`选片助手/test_reference_browser.py` 的 `test_debounce_composition_stale_response_and_task_switch_preserve_reference` 在真实 Chrome 中派发组合事件及 `InputEvent(isComposing=true)`：超过 300 ms 仍没有查询，组合结束后只查询一次。旧版直接属性处理器在该回归中失败，改用事件监听后通过。
