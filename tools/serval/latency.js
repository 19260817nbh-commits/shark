'use strict';
(() => {
  const get=id=>document.getElementById(id);
  function axis(name,value){
    get('lag-'+name).textContent=value?.status==='ok'?'约 '+Number(value.delay_ms.toFixed(2))+' ms':'—';
    get('lag-'+name+'-note').textContent=value?.status==='ok'?
      '相关度 '+value.correlation.toFixed(3)+' · 原始采样约 '+value.sample_interval_ms.toFixed(2)+' ms · 搜索步长 '+value.search_step_ms.toFixed(2)+' ms':value?.reason??'等待数据';
    get('lag-'+name).title=value?.range_ms?
      '相近匹配范围 '+value.range_ms.join('–')+' ms（非统计置信区间）':'';
  }
  async function poll(){
    try{
      const start=performance.now();
      const response=await fetch('/api/latency',{signal:AbortSignal.timeout(5000)});
      if(!response.ok)throw new Error('HTTP '+response.status);
      const data=await response.json();
      get('web-rtt').textContent=(performance.now()-start).toFixed(1)+' ms';
      axis('yaw',data.following.yaw);axis('pitch',data.following.pitch);
      const age=d=>data.observed_age_ms[d]===null?'—':Math.round(data.observed_age_ms[d])+' ms';
      get('packet-age').textContent=data.mode==='replay'?'离线回放':age('tx')+' / '+age('rx');
      get('latency-note').textContent='按 '+data.baud+' 波特、8N1 理论计算，TX 线上发送约 '+
        data.wire_time_ms.tx.toFixed(3)+' ms，RX 约 '+data.wire_time_ms.rx.toFixed(3)+
        ' ms；这不包含排队或执行。滞后使用约 5 秒有效曲线，按采样率自适应细化搜索；步长不代表测量精度。静止、未控制或相关性不足时不报数值。';
    }catch(error){
      get('web-rtt').textContent='连接失败';
      for(const name of ['yaw','pitch'])axis(name,{reason:'服务未连接，暂无有效延迟'});
    }
    setTimeout(poll,1000);
  }
  poll();
})();
