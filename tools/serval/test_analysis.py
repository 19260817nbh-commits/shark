import math
import unittest
from analysis import aligned_errors, response_summary, step_response


def frame(t,direction,yaw,mode=1):
    return dict(t=t,direction=direction,values=dict(yaw=yaw,pitch=yaw,mode=mode))


class AnalysisTests(unittest.TestCase):
    def test_error_never_uses_future_commands(self):
        data=[frame(0,'tx',1),frame(.01,'rx',.9),frame(.011,'tx',5)]
        error=aligned_errors(data,0,1)[0]
        self.assertAlmostEqual(error['yaw'],.1)
        self.assertEqual(error['tx_t'],0)

    def test_wrap_idle_and_stale_command(self):
        data=[frame(0,'tx',math.radians(179)),frame(.01,'rx',math.radians(-179)),
              frame(.02,'tx',0,0),frame(.021,'rx',.3),frame(.2,'rx',.3)]
        result=aligned_errors(data,0,1)
        self.assertEqual(len(result),1)
        self.assertAlmostEqual(result[0]['yaw'],math.radians(-2))

    def test_known_rmse_and_interval_statistics(self):
        data=[frame(0,'tx',0),frame(.01,'rx',1),frame(.02,'tx',0),frame(.03,'rx',2)]
        result=response_summary(data,0,1)
        self.assertAlmostEqual(result['yaw']['rmse'],math.sqrt(2.5))
        self.assertAlmostEqual(result['yaw']['bias'],-1.5)
        self.assertEqual(result['yaw']['max_abs'],2)
        self.assertAlmostEqual(result['sampling']['tx']['median_ms'],20)
        self.assertEqual(result['samples'],2)

    def test_step_measures_overshoot_and_refuses_moving_setpoint(self):
        data=[]
        for i in range(201):
            t=i*.01
            command=0 if t<.5 else 1
            actual=0 if t<.5 else (min(1.2,(t-.5)*4) if t<.8 else max(1.,1.2-(t-.8)))
            data.extend([frame(t,'tx',command),frame(t,'rx',actual)])
        result=step_response(data,0,2,'pitch')
        self.assertEqual(result['status'],'ok')
        self.assertAlmostEqual(result['overshoot_percent'],20)
        self.assertIsNotNone(result['rise_ms'])
        self.assertIsNotNone(result['settling_ms'])
        for f in data:
            if f['direction']=='tx' and f['t']>.5:
                f['values']['pitch']+=f['t']*.2
        self.assertEqual(step_response(data,0,2,'pitch')['status'],'unavailable')


if __name__=='__main__':
    unittest.main()
