'use strict';
(() => {
  const get=id=>document.getElementById(id);
  const colors={red:'红色',blue:'蓝色',purple:'紫色',extinguish:'熄灭'};
  const states={lost:'丢失',detecting:'确认中',tracking:'跟踪中',temp_lost:'短暂丢失',switching:'切换中'};
  let paused=false;
  function renderFps(data){
    const value=get('vision-fps'),detail=get('vision-fps-detail');
    const item=data.latest.fps;
    const live=data.mode!=='demo'&&data.mode!=='replay';
    const fresh=live&&!data.error&&item&&item.age_s<=3;
    value.textContent=fresh?item.fields.fps.toFixed(2):'—';
    value.classList.toggle('fps-stale',!fresh);
    detail.textContent=!live?'演示 / 回放无实时视觉 FPS':
      data.error?'帧率日志读取失败':
      !item?'等待自瞄帧率日志':
      !fresh?'更新已过期 · '+item.age_s.toFixed(1)+' 秒前':
      item.fields.program+' · '+item.age_s.toFixed(1)+' 秒前';
  }
  function render(data){
    const aim=data.latest.aim,control=data.latest.control;
    const a=aim?.fields,c=control?.fields,tx=data.wire.tx;
    const freshAim=aim&&aim.age_s<=3,freshControl=control&&control.age_s<=3;
    get('diagnostic-reason').textContent=data.explanation.title;
    get('diagnostic-reason').classList.toggle('diagnostic-ok',data.explanation.code==='sent');
    get('diagnostic-detail').textContent=data.explanation.detail;
    get('diagnostic-counts').textContent=freshAim?[a.detected,a.kept,a.targets].join(' / '):'—';
    get('diagnostic-colors').textContent=freshAim?a.red+' / '+a.blue:'—';
    get('diagnostic-active-color').textContent=freshAim?(colors[a.active_color]??a.active_color):'—';
    get('diagnostic-tracker').textContent=freshAim?(states[a.tracker]??a.tracker):'—';
    get('diagnostic-control').textContent=freshControl?(c.control?'允许':'不允许'):'—';
    get('diagnostic-wire').textContent=tx&&tx.age_s<=1?tx.values.mode+' / '+(tx.values.target_id??'—'):'—';
    const age=value=>value?value.age_s.toFixed(1)+' 秒前':'未收到';
    get('diagnostic-age').textContent='检测 '+age(aim)+' · 规划 '+age(control)+'；不同线程每秒采样，不能当作逐帧同步状态。';
    get('diagnostic-history').textContent=data.history.map(item=>item.text).join('\n')||'等待关键诊断日志';
    get('diagnostic-source').textContent=data.error?'日志读取失败：'+data.error:
      data.path?'只读日志：'+data.path:'未指定诊断日志。使用 orin_watch.py，或为 monitor.py 添加 --diagnostics 日志路径。';
  }
  async function poll(){
    try{
      const response=await fetch('/api/diagnostics',{signal:AbortSignal.timeout(4000)});
      if(!response.ok)throw new Error('HTTP '+response.status);
      const data=await response.json();
      renderFps(data);
      if(!paused)render(data);
    }catch(error){
      get('vision-fps').textContent='—';
      get('vision-fps').classList.add('fps-stale');
      get('vision-fps-detail').textContent='帧率服务未连接';
      if(!paused){
        get('diagnostic-reason').textContent='诊断服务未连接';
        get('diagnostic-detail').textContent=error.message;
      }
    }
    setTimeout(poll,500);
  }
  get('diagnostic-pause').addEventListener('click',()=>{
    paused=!paused;
    get('diagnostic-pause').textContent=paused?'继续诊断显示':'暂停诊断显示';
    get('diagnostic-pause').classList.toggle('active',paused);
  });
  poll();
})();
