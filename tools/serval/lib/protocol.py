"""SP protocol used by io/gimbal/gimbal.hpp (packed, little endian, radians)."""
import math
import struct

RX = struct.Struct('<2sB9fHBH')  # GimbalToVision: 44 bytes, includes enemy_color
TX = struct.Struct('<2sB6fBH')   # VisionToGimbal: 30 bytes, includes target_id
FIELDS = ('yaw', 'pitch', 'yaw_vel', 'pitch_vel', 'yaw_acc', 'pitch_acc',
          'q_yaw', 'q_pitch', 'q_roll', 'qw', 'qx', 'qy', 'qz', 'q_norm',
          'bullet_speed', 'bullet_count', 'mode', 'target_id', 'enemy_color')


class ProtocolError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _crc16_table():
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x8408 if crc & 1 else 0)
        table.append(crc)
    return tuple(table)


_CRC16 = _crc16_table()


def crc16(data):
    crc = 0xffff
    for byte in data:
        crc = (crc >> 8) ^ _CRC16[(crc ^ byte) & 0xff]
    return crc


def check_crc16(packet):
    return len(packet) >= 3 and crc16(packet[:-2]) == int.from_bytes(packet[-2:], 'little')


def decode_frame(packet, *, verified=False):
    """Decode one SP frame.

    ``verified=True`` means the caller already ran :func:`check_crc16` on the
    same bytes (e.g. the UDP fast path), so the CRC is not recomputed here.
    """
    if len(packet) not in (RX.size, TX.size) or packet[:2] != b'SP':
        raise ProtocolError('length', '帧头或长度错误：本工程 TX=30、RX=44 字节')
    if not verified and not check_crc16(packet):
        raise ProtocolError('crc', 'CRC 校验失败，请检查协议版本、波特率和线路')
    if len(packet) == RX.size:
        _, mode, w, x, y, z, yaw, vyaw, pitch, vpitch, speed, count, color, _ = RX.unpack(packet)
        if mode > 3:
            raise ProtocolError('mode', '回传模式错误')
        values = dict(yaw=yaw, pitch=pitch, yaw_vel=vyaw, pitch_vel=vpitch,
                      qw=w, qx=x, qy=y, qz=z, bullet_speed=speed, bullet_count=count,
                      mode=mode, enemy_color=color)
        norm = math.sqrt(w*w + x*x + y*y + z*z)
        values['q_norm'] = norm
        if math.isfinite(norm) and norm > 1e-9:
            w, x, y, z = (v / norm for v in (w, x, y, z))
            values.update(q_yaw=math.atan2(2*(w*z + x*y), 1-2*(y*y+z*z)),
                          q_pitch=math.asin(max(-1, min(1, 2*(w*y-z*x)))),
                          q_roll=math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y)))
        direction = 'rx'
    else:
        _, mode, yaw, vyaw, ayaw, pitch, vpitch, apitch, target_id, _ = TX.unpack(packet)
        if mode > 2:
            raise ProtocolError('mode', '指令模式错误')
        values = dict(yaw=yaw, pitch=pitch, yaw_vel=vyaw, pitch_vel=vpitch,
                      yaw_acc=ayaw, pitch_acc=apitch, mode=mode, target_id=target_id)
        direction = 'tx'
    if not all(math.isfinite(v) for v in values.values()):
        raise ProtocolError('nonfinite', '数据含 NaN 或 Infinity')
    return direction, values


class StreamDecoder:
    """Observe chunks without ever modifying the bytes forwarded by the bridge."""
    def __init__(self, direction, on_frame, on_error, on_raw=None):
        if direction not in ('rx', 'tx'):
            raise ValueError('Unknown direction')
        self.direction = direction
        self.size = RX.size if direction == 'rx' else TX.size
        self.on_frame, self.on_error = on_frame, on_error
        self.on_raw = on_raw
        self.buffer = bytearray()

    def reset(self):
        self.buffer.clear()

    def feed(self, data, timestamp=None):
        self.buffer.extend(data)
        while len(self.buffer) >= 2:
            index = self.buffer.find(b'SP')
            if index < 0:
                self.buffer[:] = self.buffer[-1:] if self.buffer[-1:] == b'S' else b''
                return
            del self.buffer[:index]
            if len(self.buffer) < self.size:
                return
            packet = bytes(self.buffer[:self.size])
            try:
                direction, values = decode_frame(packet)
            except ProtocolError as exc:
                if self.on_raw:
                    self.on_raw(self.direction, packet, timestamp, exc.code != 'crc')
                self.on_error(exc)
                del self.buffer[0]
            else:
                del self.buffer[:self.size]
                if self.on_raw:
                    self.on_raw(direction, packet, timestamp, True)
                if timestamp is None:
                    self.on_frame(direction, values)
                else:
                    self.on_frame(direction, values, timestamp)
