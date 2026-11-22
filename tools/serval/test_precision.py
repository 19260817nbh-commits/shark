import json
import csv
import io
import math
from pathlib import Path
import struct
import tempfile
import unittest
from monitor import Application
from protocol import decode_frame, crc16
from test_packets import log_record
from test_serval import wait_until


class PrecisionTests(unittest.TestCase):
    def test_field_offsets_tiny_values_sign_and_float32_roundtrip(self):
        # Populate independent offsets rather than using the decoder's packet struct.
        raw=bytearray(30);raw[:3]=b'SP\x01';raw[27]=9
        cases={'yaw':(3,-3.1415925),'yaw_vel':(7,1.234567e-8),'yaw_acc':(11,-12345.25),
               'pitch':(15,-0.00000012),'pitch_vel':(19,0.125),'pitch_acc':(23,65536.5)}
        for offset,value in cases.values():struct.pack_into('<f',raw,offset,value)
        struct.pack_into('<H',raw,28,crc16(raw[:28]))
        _,values=decode_frame(raw)
        values=json.loads(json.dumps(values))
        for name,(offset,_) in cases.items():
            self.assertEqual(struct.pack('<f',values[name]),bytes(raw[offset:offset+4]))
            # Nine significant decimal digits must preserve every float32 bit.
            self.assertEqual(struct.pack('<f',float(format(values[name],'.9g'))),bytes(raw[offset:offset+4]))

    def test_rx_all_fields_and_unsigned_counter(self):
        raw=bytearray(44);raw[:3]=b'SP\x01'
        names=['qw','qx','qy','qz','yaw','yaw_vel','pitch','pitch_vel','bullet_speed']
        values=[.5,-.5,.5,-.5,-math.pi,1e-7,-.12345,-.004,25.5]
        for i,value in enumerate(values):struct.pack_into('<f',raw,3+4*i,value)
        struct.pack_into('<H',raw,39,65535);raw[41]=0
        struct.pack_into('<H',raw,42,crc16(raw[:42]))
        _,decoded=decode_frame(raw)
        for i,name in enumerate(names):
            self.assertEqual(struct.pack('<f',decoded[name]),bytes(raw[3+4*i:7+4*i]))
        self.assertEqual(decoded['bullet_count'],65535)
        self.assertEqual(decoded['enemy_color'],0)

    def test_original_nanoseconds_preserved_and_io_failed_tx_excluded_from_measurement(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'capture.bin'
            raw=bytearray(30);raw[:3]=b'SP\x01'
            struct.pack_into('<H',raw,28,crc16(raw[:28]))
            ns=12345678901234567
            p.write_bytes(log_record('tx',raw,ns=ns,status=5))
            app=Application()
            try:
                app.start_follow(p)
                wait_until(lambda:len(app.store.packets)==1)
                item=app.packet_snapshot('tx',1)['packets'][0]
                self.assertEqual(item['log_time_ns'],str(ns))
                self.assertTrue(item['crc_ok'])
                self.assertEqual(len(app.store.frames),0)
                self.assertEqual(app.store.errors['record_status'],1)
            finally:app.close()

    def test_csv_retains_original_log_time_and_full_float_value(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'capture.bin'
            raw=bytearray(30);raw[:3]=b'SP\x01'
            struct.pack_into('<f',raw,3,1.23456789e-8)
            struct.pack_into('<H',raw,28,crc16(raw[:28]))
            ns=12345678901234567
            p.write_bytes(log_record('tx',raw,ns=ns,seq=78))
            app=Application()
            try:
                app.start_follow(p)
                wait_until(lambda:len(app.store.frames)==1)
                row=next(csv.DictReader(io.StringIO(app.store.export().decode('utf-8-sig'))))
                self.assertEqual(row['original_log_time_ns'],str(ns))
                self.assertEqual(row['log_sequence'],'78')
                self.assertEqual(struct.pack('<f',float(row['yaw'])),bytes(raw[3:7]))
            finally:app.close()


if __name__=='__main__':unittest.main()
