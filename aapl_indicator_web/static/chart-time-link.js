/* Time-based cross-chart navigation only; never changes trading signals. */
(function(root){
  function overlappingRange(bars,start,end){
    let lo=0,hi=bars.length;
    while(lo<hi){const m=(lo+hi)>>1;if(Number(bars[m].end_ts)<=start)lo=m+1;else hi=m}
    const first=lo;lo=first;hi=bars.length;
    while(lo<hi){const m=(lo+hi)>>1;if(Number(bars[m].start_ts)<end)lo=m+1;else hi=m}
    return first<lo?[first,lo-1]:null;
  }
  function centeredWindow(length,first,last,span){
    const width=Math.min(length,Math.max(last-first+1,span));
    const start=Math.max(0,Math.min(length-width,Math.round((first+last-width+1)/2)));
    return [start,start+width-1];
  }
  function install({charts,settings,getBars,isVisible,formatTime,onZoom}){
    if(!settings?.enabled)return {decorateOption:(_i,_b,o)=>o};
    let selected=null;
    const intervals=settings.intervals;
    function validSelection(){
      if(selected&&getBars(selected.interval)!==selected.sourceBars)selected=null;
      return selected;
    }
    function markerSeries(interval,bars){
      const selection=validSelection(),range=selection?overlappingRange(bars,selection.start,selection.end):null;
      const smaller=range&&Number(bars[range[0]].end_ts)-Number(bars[range[0]].start_ts)<selection.end-selection.start;
      const points=range?(smaller&&range[0]!==range[1]?[range[0],range[1]]:[range[0]]):[];
      const lines=points.map((x,j)=>({xAxis:x,name:smaller?(j?'区间最后一根':'区间第一根'):'对应 K 线'}));
      const make=(volume)=>({id:volume?'linked-time-volume':'linked-time-price',name:volume?'联动成交量定位':'联动时间定位',type:'line',data:[],silent:true,xAxisIndex:volume?1:0,yAxisIndex:volume?1:0,z:20,
        markLine:{silent:true,symbol:['none','none'],animation:false,lineStyle:{color:settings.line_color,width:settings.line_width,type:'dashed'},label:{show:false},data:lines},
        markArea:{silent:true,label:{show:false},itemStyle:{color:settings.area_color},data:smaller&&range[0]!==range[1]?[[{xAxis:range[0]},{xAxis:range[1]}]]:[]}});
      return [make(false),make(true)];
    }
    function select(interval,index){
      const sourceBars=getBars(interval),bar=sourceBars[index];if(!bar)return;
      selected={interval,sourceBars,start:Number(bar.start_ts),end:Number(bar.end_ts)};
      for(const target of intervals){
        const bars=getBars(target),chart=charts[target];if(!bars.length||!isVisible(target))continue;
        const range=overlappingRange(bars,selected.start,selected.end);
        chart.setOption({series:markerSeries(target,bars)});
        const detail=document.getElementById(`link-detail-${target}`);
        if(detail)detail.textContent=range?`${interval} 选中 ${formatTime(selected.start)} — ${formatTime(selected.end)}（右端不含）；${target} 对应 ${range[1]-range[0]+1} 根：${formatTime(bars[range[0]].start_ts)} — ${formatTime(bars[range[1]].end_ts)}`:'选中区间在本周期没有数据';
        if(!range||target===interval)continue;
        const zoom=chart.getOption().dataZoom?.[0]||{},currentSpan=Math.round((Number(zoom.end)-Number(zoom.start))/100*(bars.length-1))+1;
        const span=Math.min(settings.maximum_visible_bars,Math.max(settings.minimum_visible_bars,currentSpan||settings.minimum_visible_bars,range[1]-range[0]+1));
        const [start,end]=centeredWindow(bars.length,...range,span);
        chart.dispatchAction({type:'dataZoom',batch:[{dataZoomIndex:0,startValue:start,endValue:end},{dataZoomIndex:1,startValue:start,endValue:end}]});
      }
    }
    for(const interval of intervals){
      const chart=charts[interval],card=document.getElementById(`card-${interval}`);
      const detail=document.createElement('div');detail.id=`link-detail-${interval}`;detail.className='detail';detail.textContent='点击 K 线联动其他周期；红色虚线定位，低周期双线标出覆盖区间';detail.setAttribute('aria-live','polite');card.appendChild(detail);
      chart.getZr().on('click',event=>{
        if(!isVisible(interval)||!getBars(interval).length)return;
        const point=[event.offsetX,event.offsetY];let axis=null;
        if(chart.containPixel({gridIndex:0},point))axis=0;else if(chart.containPixel({gridIndex:1},point))axis=1;
        if(axis===null)return;
        const index=Math.round(chart.convertFromPixel({xAxisIndex:axis},event.offsetX));
        if(Number.isFinite(index)&&index>=0&&index<getBars(interval).length)select(interval,index);
      });
      chart.on('datazoom',()=>{const z=chart.getOption().dataZoom?.[0];if(z)onZoom(interval,z.start,z.end)});
    }
    return {select,decorateOption(interval,bars,option){
      if(intervals.includes(interval)){
        const hadSelection=!!selected;validSelection();
        if(hadSelection&&!selected)for(const i of intervals){const d=document.getElementById(`link-detail-${i}`);if(d)d.textContent='点击 K 线联动其他周期；红色虚线定位，低周期双线标出覆盖区间'}
        // Keep helper overlays out of the indicator legend.
        option.legend.data=option.series.map(s=>s.name);
        option.series.push(...markerSeries(interval,bars));
      }
      return option;
    }};
  }
  root.ChartTimeLink={overlappingRange,centeredWindow,install};
  if(typeof module!=='undefined')module.exports=root.ChartTimeLink;
})(typeof window!=='undefined'?window:globalThis);
