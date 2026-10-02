/**
 * A Lightweight Charts series primitive that draws continuity breaks as hatched vertical
 * bands with their cause (ADR-0014/0017). Nothing is drawn *across* a break: the band
 * sits in the gap between the last bar of one segment and the first of the next.
 */

import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  Time,
} from "lightweight-charts";
import type { BreakBand } from "@/lib/chart-model";

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];

export class BreakBands implements ISeriesPrimitive<Time> {
  private chart: IChartApi | null = null;
  private readonly views: IPrimitivePaneView[];

  constructor(
    private readonly bands: BreakBand[],
    private readonly color: string,
    private readonly textColor: string,
  ) {
    this.views = [
      {
        zOrder: (): PrimitivePaneViewZOrder => "bottom",
        renderer: (): IPrimitivePaneRenderer => ({ draw: (target) => this.draw(target) }),
      },
    ];
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart as IChartApi;
  }

  detached(): void {
    this.chart = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  private draw(target: Target): void {
    const chart = this.chart;
    if (!chart) return;
    const scale = chart.timeScale();
    const spacing = scale.options().barSpacing;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      for (const band of this.bands) {
        const prev = scale.timeToCoordinate(band.previousTime as Time);
        const next = scale.timeToCoordinate(band.time as Time);
        if (prev === null || next === null) continue;
        // The gap between the two segments, widened to stay visible when bars are dense.
        const mid = (prev + next) / 2;
        const half = Math.max((next - prev - spacing) / 2, 5);
        const left = mid - half;
        const right = mid + half;
        ctx.save();
        ctx.beginPath();
        ctx.rect(left, 0, right - left, mediaSize.height);
        ctx.clip();
        ctx.globalAlpha = 0.14;
        ctx.fillStyle = this.color;
        ctx.fillRect(left, 0, right - left, mediaSize.height);
        ctx.globalAlpha = 0.45;
        ctx.strokeStyle = this.color;
        ctx.lineWidth = 1;
        for (let x = left - mediaSize.height; x < right; x += 7) {
          ctx.beginPath();
          ctx.moveTo(x, mediaSize.height);
          ctx.lineTo(x + mediaSize.height, 0);
          ctx.stroke();
        }
        ctx.restore();
        ctx.save();
        ctx.fillStyle = this.textColor;
        ctx.font = "500 11px 'IBM Plex Sans', system-ui, sans-serif";
        ctx.translate(right + 4, 14);
        ctx.fillText(`Break: ${band.cause}`, 0, 0);
        ctx.restore();
      }
    });
  }
}
