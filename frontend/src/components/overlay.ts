/**
 * Draws placed layer objects (ADR-0027) on one pane of the chart. It maps each stored
 * (date, value) to pixels through the chart's own time and price scales and draws
 * straight segments between consecutive stored points; it never adds a point, extends a
 * line past its last point, or draws an unbounded price line. Time-only marks sit along
 * the bottom of the pane. Hit-testing reports the object under the cursor by its id.
 */

import type {
  Coordinate,
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  PrimitiveHoveredItem,
  PrimitivePaneViewZOrder,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import type { Drawn, Pane, Primitive, Role } from "@/lib/layers/types";

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];

export type RoleStyle = { stroke: string; fill?: string; width?: number };
export type RoleStyles = Record<Role, RoleStyle>;

type Hit = { id: string; distance: number; priority: number };

/** Short structure labels stay on; other text appears for the object under focus, so
 * many overlapping objects stay legible. */
const ALWAYS_LABELLED: ReadonlySet<Role> = new Set<Role>(["label", "bos", "choch"]);

const TICK_ROW = 14; // px from the bottom of the pane for time-only marks
const SPAN_ROW = 5;

export class Overlay implements ISeriesPrimitive<Time> {
  private chart: IChartApi | null = null;
  private series: ISeriesApi<SeriesType> | null = null;
  private requestUpdate: (() => void) | null = null;
  private readonly views: IPrimitivePaneView[];
  private objects: readonly Drawn[] = [];
  private highlighted: string | null = null;
  private lastSize = { width: 0, height: 0 };

  constructor(
    private readonly pane: Pane,
    private readonly styles: RoleStyles,
    private readonly font: string,
  ) {
    this.views = [
      {
        zOrder: (): PrimitivePaneViewZOrder => "top",
        renderer: (): IPrimitivePaneRenderer => ({ draw: (target) => this.draw(target) }),
      },
    ];
  }

  setObjects(objects: readonly Drawn[], highlighted: string | null): void {
    this.objects = objects;
    this.highlighted = highlighted;
    this.requestUpdate?.();
  }

  highlight(id: string | null): void {
    if (id === this.highlighted) return;
    this.highlighted = id;
    this.requestUpdate?.();
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart as IChartApi;
    this.series = param.series as ISeriesApi<SeriesType>;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
    this.requestUpdate = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  private x(date: string): number | null {
    return this.chart?.timeScale().timeToCoordinate(date as Time) ?? null;
  }

  private y(value: number): number | null {
    return this.series?.priceToCoordinate(value) ?? null;
  }

  private mine(p: Primitive): boolean {
    if (p.kind === "tick" || p.kind === "span") return this.pane === "price";
    return p.pane === this.pane;
  }

  private draw(target: Target): void {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      this.lastSize = { width: mediaSize.width, height: mediaSize.height };
      ctx.save();
      ctx.font = this.font;
      ctx.lineJoin = "round";
      // Bands first, then lines, then points, then labels: points stay readable.
      for (const pass of ["box", "span", "path", "dot", "tick"] as const) {
        for (const o of this.objects) {
          const strong = this.highlighted === o.id;
          for (const p of o.primitives) {
            if (p.kind !== pass || !this.mine(p)) continue;
            this.drawOne(ctx, p, strong, mediaSize.height);
          }
        }
      }
      ctx.restore();
    });
  }

  private drawOne(ctx: CanvasRenderingContext2D, p: Primitive, strong: boolean, height: number): void {
    const style = this.styles[p.role];
    switch (p.kind) {
      case "path": {
        const pts = p.points.map((c) => [this.x(c.date), this.y(c.value)] as const);
        if (pts.some(([x, y]) => x === null || y === null)) return;
        ctx.beginPath();
        ctx.strokeStyle = style.stroke;
        ctx.lineWidth = (style.width ?? 1.25) + (strong ? 1.25 : 0);
        ctx.setLineDash(p.dashed ? [5, 4] : []);
        pts.forEach(([x, y], i) => (i === 0 ? ctx.moveTo(x!, y!) : ctx.lineTo(x!, y!)));
        if (pts.length === 1) ctx.arc(pts[0]![0]!, pts[0]![1]!, 1.5, 0, 2 * Math.PI);
        ctx.stroke();
        ctx.setLineDash([]);
        if (p.label && pts.length > 1 && strong) {
          const [x, y] = pts.at(-1)!;
          ctx.fillStyle = style.stroke;
          ctx.fillText(p.label, x! + 4, y! + 3);
        }
        return;
      }
      case "box": {
        const x1 = this.x(p.from.date);
        const x2 = this.x(p.to.date);
        const y1 = this.y(p.from.value);
        const y2 = this.y(p.to.value);
        if (x1 === null || x2 === null || y1 === null || y2 === null) return;
        ctx.fillStyle = style.fill ?? style.stroke;
        ctx.globalAlpha = strong ? 0.32 : 0.16;
        ctx.fillRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.max(Math.abs(y2 - y1), 1));
        ctx.globalAlpha = 1;
        ctx.strokeStyle = style.stroke;
        ctx.lineWidth = strong ? 1.5 : 0.75;
        ctx.strokeRect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.max(Math.abs(y2 - y1), 1));
        return;
      }
      case "dot": {
        const x = this.x(p.at.date);
        const y = this.y(p.at.value);
        if (x === null || y === null) return;
        ctx.beginPath();
        ctx.arc(x, y, strong ? 4.5 : 3, 0, 2 * Math.PI);
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = style.stroke;
        ctx.fillStyle = style.stroke;
        if (p.hollow) ctx.stroke();
        else ctx.fill();
        if (p.text && (strong || ALWAYS_LABELLED.has(p.role))) {
          ctx.fillStyle = style.stroke;
          const w = ctx.measureText(p.text).width;
          ctx.fillText(p.text, x - w / 2, p.above ? y - 7 : y + 14);
        }
        return;
      }
      case "tick": {
        const x = this.x(p.date);
        if (x === null) return;
        const base = height - TICK_ROW;
        ctx.beginPath();
        ctx.fillStyle = style.stroke;
        ctx.moveTo(x, base - 6);
        ctx.lineTo(x - 4, base);
        ctx.lineTo(x + 4, base);
        ctx.closePath();
        ctx.fill();
        if (strong) ctx.fillText(p.text, x + 6, base);
        return;
      }
      case "span": {
        const x1 = this.x(p.from);
        const x2 = this.x(p.to);
        if (x1 === null || x2 === null) return;
        ctx.fillStyle = style.fill ?? style.stroke;
        ctx.globalAlpha = strong ? 0.9 : 0.55;
        ctx.fillRect(x1, height - SPAN_ROW, Math.max(x2 - x1, 1), SPAN_ROW - 1);
        ctx.globalAlpha = 1;
        return;
      }
    }
  }

  hitTest(x: number, y: number): PrimitiveHoveredItem | null {
    let best: Hit | null = null;
    const consider = (id: string, distance: number, priority: number) => {
      if (distance > 6) return;
      if (!best || priority > best.priority || (priority === best.priority && distance < best.distance)) {
        best = { id, distance, priority };
      }
    };
    const height = this.lastSize.height;
    for (const o of this.objects) {
      for (const p of o.primitives) {
        if (!this.mine(p)) continue;
        if (p.kind === "dot") {
          const px = this.x(p.at.date);
          const py = this.y(p.at.value);
          if (px !== null && py !== null) consider(o.id, Math.hypot(px - x, py - y), 2);
        } else if (p.kind === "tick") {
          const px = this.x(p.date);
          if (px !== null) consider(o.id, Math.hypot(px - x, height - TICK_ROW - 3 - y), 2);
        } else if (p.kind === "path") {
          for (let i = 1; i < p.points.length; i++) {
            const a = p.points[i - 1]!;
            const b = p.points[i]!;
            const ax = this.x(a.date);
            const ay = this.y(a.value);
            const bx = this.x(b.date);
            const by = this.y(b.value);
            if (ax === null || ay === null || bx === null || by === null) continue;
            consider(o.id, segmentDistance(x, y, ax, ay, bx, by), 1);
          }
        } else if (p.kind === "box") {
          const x1 = this.x(p.from.date);
          const x2 = this.x(p.to.date);
          const y1 = this.y(p.from.value);
          const y2 = this.y(p.to.value);
          if (x1 === null || x2 === null || y1 === null || y2 === null) continue;
          const inside = x >= Math.min(x1, x2) && x <= Math.max(x1, x2) && y >= Math.min(y1, y2) - 2 && y <= Math.max(y1, y2) + 2;
          if (inside) consider(o.id, 0, 0);
        } else if (p.kind === "span") {
          const x1 = this.x(p.from);
          const x2 = this.x(p.to);
          if (x1 !== null && x2 !== null && x >= x1 && x <= x2 && y >= height - SPAN_ROW - 4) consider(o.id, 0, 0);
        }
      }
    }
    const found = best as Hit | null;
    return found
      ? { externalId: found.id, zOrder: "top", distance: found.distance, hitTestPriority: found.priority, itemType: "primitive" }
      : null;
  }
}

function segmentDistance(px: number, py: number, ax: Coordinate | number, ay: number, bx: number, by: number): number {
  const dx = bx - ax;
  const dy = by - ay;
  const len = dx * dx + dy * dy;
  const t = len === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / len));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}
