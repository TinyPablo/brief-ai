(function () {
  const palette = ['#e6a84a', '#6cc48a', '#8caee8', '#d499c7'];
  const number = (value) => new Intl.NumberFormat(undefined, { maximumSignificantDigits: 8 }).format(value);
  const tickNumber = (value) => new Intl.NumberFormat(undefined, { notation: Math.abs(value) >= 10000 ? 'compact' : 'standard', maximumSignificantDigits: 3 }).format(value);
  const shorten = (text, limit) => String(text).length > limit ? String(text).slice(0, limit - 1) + '…' : String(text);

  function Chart({ chart }) {
    const container = React.useRef(null);
    const [width, setWidth] = React.useState(600);
    const [hidden, setHidden] = React.useState([]);
    const [selected, setSelected] = React.useState(null);
    const [showData, setShowData] = React.useState(false);
    const chartId = React.useId();
    React.useEffect(() => {
      if (!container.current) return;
      const observer = new ResizeObserver(([entry]) => setWidth(Math.max(200, entry.contentRect.width)));
      observer.observe(container.current);
      return () => observer.disconnect();
    }, [chart]);
    const valid = chart?.version === 1 && ['bar', 'line', 'scatter'].includes(chart.type) &&
      Array.isArray(chart.series) && chart.series.length > 0 && chart.series.length <= 4 &&
      ['title', 'x_label', 'y_label', 'assumptions'].every((key) => typeof chart[key] === 'string') &&
      chart.series.every((s) => s && typeof s.name === 'string' && Array.isArray(s.points) && s.points.length > 0 &&
        s.points.every((p) => p && Number.isFinite(p.y) && (typeof p.x === 'string' || Number.isFinite(p.x)))) &&
      chart.series.reduce((total, s) => total + s.points.length, 0) <= 400;
    if (!valid) return <p className="chart-unavailable" role="status">This chart is unavailable.</p>;
    const series = chart.series.map((s, index) => ({ ...s, index, color: palette[index] }));
    const visible = series.filter((s) => !hidden.includes(s.index));
    const all = series.flatMap((s) => s.points);
    const numericX = chart.type !== 'bar' && typeof all[0].x === 'number';
    const categories = [...new Set(all.map((p) => p.x))];
    const plot = { left: 64, right: width - 22, top: 22, bottom: 238 };
    const plotWidth = Math.max(1, plot.right - plot.left);
    const yValues = all.map((p) => p.y);
    let yMin = Math.min(...yValues), yMax = Math.max(...yValues);
    if (chart.type === 'bar') { yMin = Math.min(0, yMin); yMax = Math.max(0, yMax); }
    const yPad = (yMax - yMin || Math.abs(yMax) || 1) * .1;
    if (chart.type !== 'bar' || yMin < 0) yMin -= yPad;
    if (chart.type !== 'bar' || yMax > 0) yMax += yPad;
    if (yMin === yMax) yMax = yMin + 1;
    const y = (value) => plot.bottom - (value - yMin) / (yMax - yMin) * (plot.bottom - plot.top);
    let xMin = 0, xMax = 1;
    if (numericX) {
      xMin = Math.min(...all.map((p) => p.x)); xMax = Math.max(...all.map((p) => p.x));
      const pad = (xMax - xMin || Math.abs(xMax) || 1) * .04;
      xMin -= pad; xMax += pad;
    }
    const x = (value) => numericX ? plot.left + (value - xMin) / (xMax - xMin) * plotWidth :
      plot.left + (categories.indexOf(value) + .5) / categories.length * plotWidth;
    const yTicks = Array.from({ length: 5 }, (_, i) => yMin + (yMax - yMin) * i / 4);
    const maxXTicks = width < 480 ? 3 : 6;
    const xTicks = numericX ? Array.from({ length: maxXTicks }, (_, i) => xMin + (xMax - xMin) * i / (maxXTicks - 1)) :
      categories.filter((_, i) => i % Math.max(1, Math.ceil(categories.length / maxXTicks)) === 0);
    const marks = visible.flatMap((s, group) => s.points.map((p) => {
      const center = x(p.x);
      if (chart.type !== 'bar') return { ...p, px: center, py: y(p.y), series: s };
      const band = plotWidth / categories.length * .78;
      const barWidth = band / Math.max(1, visible.length);
      return { ...p, px: center - band/2 + group*barWidth + barWidth/2, py:y(p.y),
        barWidth: Math.max(.5, barWidth - Math.min(3, barWidth*.1)), series:s };
    }));
    const active = selected && marks.find((m) => m.series.index === selected.series && m.x === selected.x && m.y === selected.y);
    const selectNearest = (event) => {
      if (!marks.length) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      const px = (event.clientX - bounds.left) * width / bounds.width;
      const py = (event.clientY - bounds.top) * 310 / bounds.height;
      if (px < plot.left || px > plot.right || py < plot.top || py > plot.bottom) return;
      const nearest = marks.reduce((best, mark) => {
        const distance = (mark.px-px)**2 + (chart.type === 'bar' ? 0 : (mark.py-py)**2);
        return !best || distance < best.distance ? {mark, distance} : best;
      }, null).mark;
      setSelected({series:nearest.series.index, x:nearest.x, y:nearest.y});
    };
    const label = typeof chart.title === 'string' ? chart.title : 'Chart';
    return <section className="answer-chart" aria-label={label}>
      <header className="answer-chart-header"><h3>{label}</h3><button type="button" className="chart-data-toggle" aria-expanded={showData} aria-controls={chartId + '-data'} onClick={() => setShowData((value) => !value)}>{showData ? 'Hide data' : 'View data'}</button></header>
      <div className="chart-provenance">{chart.source === 'example' ? 'Illustrative data' : 'Conversation data'}{chart.assumptions ? ' · ' + chart.assumptions : ''}</div>
      <div className="chart-legend" aria-label="Chart series">{series.map((s) => <button type="button" key={s.index} aria-pressed={!hidden.includes(s.index)} onClick={() => {setHidden((previous) => previous.includes(s.index) ? previous.filter((i) => i !== s.index) : [...previous, s.index]); setSelected(null);}}><span style={{background:s.color}} aria-hidden="true" />{s.name}</button>)}</div>
      <div ref={container} className="chart-plot">
        <svg viewBox={'0 0 ' + width + ' 310'} width="100%" height="310" role="img" aria-labelledby={chartId+'-title '+chartId+'-description'} onPointerMove={selectNearest} onPointerDown={selectNearest}>
          <title id={chartId+'-title'}>{label}</title><desc id={chartId+'-description'}>{chart.type} chart. {chart.x_label}; {chart.y_label}. Use View data for all values and series buttons to change visibility.</desc>
          <defs><clipPath id={chartId+'-clip'}><rect x={plot.left} y={plot.top} width={plotWidth} height={plot.bottom-plot.top}/></clipPath></defs>
          {yTicks.map((value, i) => <g key={i}><line x1={plot.left} x2={plot.right} y1={y(value)} y2={y(value)} className="chart-grid"/><text x={plot.left-9} y={y(value)+4} textAnchor="end" className="chart-tick">{tickNumber(value)}</text></g>)}
          <line x1={plot.left} x2={plot.right} y1={plot.bottom} y2={plot.bottom} className="chart-axis"/>
          {xTicks.map((value, i) => <text key={i} x={x(value)} y={plot.bottom+22} textAnchor="middle" className="chart-tick"><title>{String(value)}</title>{numericX ? tickNumber(value) : shorten(value, width < 480 ? 10 : 15)}</text>)}
          <text x={plot.left} y={12} className="chart-unit"><title>{chart.y_label}</title>{shorten(chart.y_label, Math.max(14,Math.floor(plotWidth/7)))}</text>
          <text x={(plot.left+plot.right)/2} y={294} textAnchor="middle" className="chart-unit"><title>{chart.x_label}</title>{shorten(chart.x_label, Math.max(14,Math.floor(plotWidth/7)))}</text>
          <g clipPath={'url(#'+chartId+'-clip)'}>
            {chart.type === 'line' && visible.map((s) => <polyline key={s.index} fill="none" stroke={s.color} strokeWidth="2" strokeDasharray={s.index % 2 ? '6 3' : undefined} points={[...s.points].sort((a,b) => numericX ? a.x-b.x : categories.indexOf(a.x)-categories.indexOf(b.x)).map((p) => x(p.x)+','+y(p.y)).join(' ')}/>)}
            {marks.map((mark, index) => chart.type === 'bar' ? <rect key={index} x={mark.px-mark.barWidth/2} y={Math.min(y(0),mark.py)} width={mark.barWidth} height={Math.max(.5,Math.abs(y(0)-mark.py))} fill={mark.series.color}><title>{mark.series.name}: {String(mark.x)}, {number(mark.y)}</title></rect> :
              <circle key={index} cx={mark.px} cy={mark.py} r={chart.type === 'scatter' ? 4 : 3} fill={mark.series.color}><title>{mark.series.name}: {String(mark.x)}, {number(mark.y)}</title></circle>)}
            {active && <><line x1={active.px} x2={active.px} y1={plot.top} y2={plot.bottom} className="chart-hover-guide"/><circle cx={active.px} cy={active.py} r="6" fill="none" stroke={active.series.color} strokeWidth="2"/></>}
          </g>
          {!visible.length && <text x={(plot.left+plot.right)/2} y="130" textAnchor="middle" className="chart-unit">Select a series to display</text>}
        </svg>
        <div className="chart-detail">{active ? <><span className="chart-detail-series" style={{color:active.series.color}}>{active.series.name}</span><span>{String(active.x)}</span><strong>{number(active.y)}</strong><span>{chart.y_label}</span></> : <span>Hover or tap to inspect values</span>}</div>
      </div>
      {showData && <div className="chart-data" id={chartId+'-data'}><table><caption>{label} - data</caption><thead><tr><th scope="col">Series</th><th scope="col">{chart.x_label}</th><th scope="col">{chart.y_label}</th></tr></thead><tbody>{series.flatMap((s) => s.points.map((p,i) => <tr key={s.index+'-'+i}><th scope="row">{s.name}</th><td>{typeof p.x === 'number' ? number(p.x) : p.x}</td><td>{number(p.y)}</td></tr>))}</tbody></table></div>}
    </section>;
  }

  function AnswerCharts({ charts }) {
    if (!Array.isArray(charts) || !charts.length) return null;
    return <div className="answer-charts">{charts.slice(0,3).map((chart,index) => <Chart key={index} chart={chart}/>)}</div>;
  }
  window.BriefCharts = { AnswerCharts };
})();
