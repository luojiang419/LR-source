"""纯离线创意输入映射：不打开设备、不发送系统输入、不连接网络。

运行：python 技术文档/示例/创意事件映射.py
校验：python 技术文档/示例/创意事件映射.py --self-test
"""
from dataclasses import dataclass
import json
import sys


@dataclass(frozen=True)
class Packet:
    state: int = 0
    rolling: int = 0
    dx: int = 0
    dy: int = 0
    flags: int = 0


class CreativeRouter:
    DOUBLE_SECONDS = .25
    LONG_SECONDS = .65

    def __init__(self):
        self.left_down_at = None
        self.long_sent = False
        self.pending_single_at = None
        self.theme = 0
        self.intensity = 50
        self.x = self.y = .5
        self.last_time = -1.0

    @staticmethod
    def event(name, **payload):
        return {'name': name, 'payload': payload}

    def _advance(self, now):
        if now < self.last_time:
            raise ValueError('时间必须单调递增')
        self.last_time = now
        output = []
        if self.left_down_at is not None and not self.long_sent and now - self.left_down_at >= self.LONG_SECONDS:
            self.long_sent = True
            output.append(self.event('charge', level=1))
        # 第二次按下及时到达时，保留待判定单击，直到它松开或不再可能双击。
        if self.pending_single_at is not None:
            second_started_in_time = self.left_down_at is not None and self.left_down_at <= self.pending_single_at
            if now >= self.pending_single_at and not second_started_in_time:
                self.pending_single_at = None
                output.append(self.event('like', amount=1))
        return output

    def tick(self, now):
        """即使没有新输入也需定期调用，才能确定延迟单击与长按。"""
        return self._advance(now)

    def feed(self, packet, now):
        output = self._advance(now)
        if packet.state & 0x0001 and self.left_down_at is None:
            self.left_down_at = now
            self.long_sent = False
        if packet.state & 0x0002 and self.left_down_at is not None:
            if not self.long_sent:
                if self.pending_single_at is not None:
                    if self.left_down_at <= self.pending_single_at:
                        self.pending_single_at = None
                        output.append(self.event('burst', hearts=8))
                    else:
                        output.append(self.event('like', amount=1))
                        self.pending_single_at = now + self.DOUBLE_SECONDS
                else:
                    self.pending_single_at = now + self.DOUBLE_SECONDS
            self.left_down_at = None
            self.long_sent = False
        if packet.state & 0x0004:
            self.theme = (self.theme + 1) % 3
            output.append(self.event('theme', index=self.theme))
        if packet.state & 0x0400:
            raw = packet.rolling & 0xFFFF
            delta = raw - 65536 if raw >= 32768 else raw
            self.intensity = max(0, min(100, round(self.intensity + delta / 120 * 5)))
            output.append(self.event('intensity', value=self.intensity))
        # 样例仅解释相对移动；绝对定位设备必须走独立坐标映射。
        if not packet.flags & 0x0001 and (packet.dx or packet.dy):
            self.x = max(0.0, min(1.0, self.x + packet.dx / 1000))
            self.y = max(0.0, min(1.0, self.y + packet.dy / 1000))
            output.append(self.event('brush', x=round(self.x, 3), y=round(self.y, 3)))
        return output

    def reset(self):
        """退出/拔插时丢弃未判定手势，不能在恢复鼠标后补发点赞。"""
        self.left_down_at = self.pending_single_at = None
        self.long_sent = False


def self_test():
    def names(events): return [event['name'] for event in events]
    r = CreativeRouter()
    assert r.feed(Packet(state=1), 0) == []
    assert r.feed(Packet(state=1), .01) == []  # 重复 DOWN 不重新开始手势
    assert r.feed(Packet(state=2), .05) == []
    assert names(r.tick(.31)) == ['like']
    r = CreativeRouter()
    for packet, now in [(Packet(state=1), 0), (Packet(state=2), .04), (Packet(state=1), .15)]:
        assert r.feed(packet, now) == []
    assert names(r.feed(Packet(state=2), .2)) == ['burst']
    assert r.tick(1) == []
    r = CreativeRouter(); r.feed(Packet(state=1), 0)
    assert names(r.tick(.65)) == ['charge']
    assert r.tick(.8) == [] and r.feed(Packet(state=2), .9) == []
    r = CreativeRouter()
    assert r.feed(Packet(state=0x400, rolling=65416), 0)[0]['payload']['value'] == 45
    assert r.feed(Packet(state=0x400, rolling=32767), .1)[0]['payload']['value'] == 100
    assert r.feed(Packet(dx=100, dy=-100), .2)[0]['payload'] == {'x': .6, 'y': .4}
    assert r.feed(Packet(dx=100, flags=1), .3) == []
    assert r.feed(Packet(state=4), .4)[0]['payload']['index'] == 1
    r.feed(Packet(state=1), .5); r.feed(Packet(state=2), .55); r.reset()
    assert r.tick(2) == []
    try:
        r.tick(1)
    except ValueError:
        pass
    else:
        raise AssertionError('时间倒退必须被拒绝')
    print('PASS: 单击、重复DOWN、双击、长按、滚轮符号/边界、相对/绝对坐标、主题、取消、单调时间')


def demo():
    r = CreativeRouter()
    replay = [
        (0.0, Packet(state=1)), (.05, Packet(state=2)), (.31, None),
        (.5, Packet(state=1)), (.55, Packet(state=2)), (.65, Packet(state=1)), (.7, Packet(state=2)),
        (1.0, Packet(state=1)), (1.7, None), (1.8, Packet(state=2)),
        (2.0, Packet(state=4)), (2.1, Packet(state=0x400, rolling=120)),
        (2.2, Packet(dx=80, dy=-40)),
    ]
    for now, packet in replay:
        for event in (r.tick(now) if packet is None else r.feed(packet, now)):
            print(json.dumps({'time': now, **event}, ensure_ascii=False))


if __name__ == '__main__':
    self_test() if '--self-test' in sys.argv else demo()
