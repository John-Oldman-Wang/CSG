import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  HistogramSeries,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { Candle } from "@/types";

/**
 * K 线图（TradingView Lightweight Charts）。
 *
 * **配色遵循 A 股惯例：红涨绿跌**，与欧美市场相反。
 * 套用库的默认配色会让每个中国用户第一眼读反。
 *
 * 传入的应为**后复权**价格：它不因未来的除权事件改变历史值。
 * 前复权仅可用于展示当前视角，且必须实时计算，绝不落盘。
 */

const UP = "#e5484d"; // 涨 —— 红
const DOWN = "#30a46c"; // 跌 —— 绿

export default function CandleChart({
  data,
  height = 380,
  markers,
}: {
  data: Candle[];
  height?: number;
  /** 事件标记，如研报发布日、财报披露日 */
  markers?: { time: string; text: string; color?: string }[];
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!ref.current || data.length === 0) return;

    const chart = createChart(ref.current, {
      height,
      layout: {
        background: { color: "transparent" },
        textColor: "#9aa0aa",
        fontFamily: "ui-monospace, monospace",
      },
      grid: {
        vertLines: { color: "rgba(255,255,255,0.04)" },
        horzLines: { color: "rgba(255,255,255,0.04)" },
      },
      rightPriceScale: { borderColor: "rgba(255,255,255,0.1)" },
      timeScale: { borderColor: "rgba(255,255,255,0.1)", rightOffset: 5 },
      crosshair: { mode: 0 },
    });
    chartRef.current = chart;

    const candles = chart.addSeries(CandlestickSeries, {
      upColor: UP,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
    });

    const toTime = (d: string) =>
      (new Date(`${d.slice(0, 10)}T00:00:00Z`).getTime() / 1000) as UTCTimestamp;

    candles.setData(
      data.map((d) => ({
        time: toTime(d.time),
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
      })),
    );

    // 成交量叠加在下方 20% 区域，与价格共用时间轴
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
    });
    chart.priceScale("vol").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });
    volume.setData(
      data.map((d) => ({
        time: toTime(d.time),
        value: d.volume,
        color: d.close >= d.open ? `${UP}55` : `${DOWN}55`,
      })),
    );

    // v5 起 series.setMarkers 已移除，改用独立的 createSeriesMarkers
    if (markers?.length) {
      createSeriesMarkers(
        candles,
        markers.map((m) => ({
          time: toTime(m.time),
          position: "aboveBar" as const,
          color: m.color ?? "#8b8f98",
          shape: "circle" as const,
          text: m.text,
        })),
      );
    }

    chart.timeScale().fitContent();

    const ro = new ResizeObserver(([entry]) => {
      chart.applyOptions({ width: entry.contentRect.width });
    });
    ro.observe(ref.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, [data, height, markers]);

  if (data.length === 0) {
    return (
      <div
        style={{ height }}
        className="flex items-center justify-center text-[var(--color-muted)] text-sm"
      >
        暂无行情数据
      </div>
    );
  }

  return <div ref={ref} style={{ height }} />;
}
