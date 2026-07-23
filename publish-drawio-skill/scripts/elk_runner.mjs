#!/usr/bin/env node
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ELK = require("../vendor/elkjs/elk.bundled.js");

const ELKJS_VERSION = "0.11.1";
const BACKEND_ID = `elk-layered-${ELKJS_VERSION}`;
const SIDES = Object.freeze({
  north: "NORTH",
  east: "EAST",
  south: "SOUTH",
  west: "WEST",
});

function stableCompare(left, right) {
  const leftText = String(left);
  const rightText = String(right);
  return leftText < rightText ? -1 : leftText > rightText ? 1 : 0;
}

function effectiveOptions(input) {
  const direction = input.direction === "LR" ? "RIGHT" : "DOWN";
  return {
    "elk.algorithm": "layered",
    "elk.direction": direction,
    "elk.edgeRouting": "ORTHOGONAL",
    "elk.layered.considerModelOrder.strategy": "NODES_AND_EDGES",
    "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
    "elk.spacing.nodeNode": String(input.constraints?.node_separation ?? 40),
    "elk.layered.spacing.nodeNodeBetweenLayers": String(input.constraints?.layer_separation ?? 80),
  };
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

function portId(nodeIndex, side) {
  return `n${nodeIndex}-p-${side}`;
}

function toElkGraph(page, input) {
  const orderedNodes = [...page.nodes].sort((a, b) => stableCompare(a.node_id, b.node_id));
  const nodeIndex = new Map(orderedNodes.map((node, index) => [node.node_id, index]));
  const children = orderedNodes.map((node, index) => ({
    id: `n${index}`,
    width: node.width,
    height: node.height,
    layoutOptions: {
      "org.eclipse.elk.portConstraints": "FIXED_SIDE",
    },
    ports: Object.keys(SIDES).map((side) => ({
      id: portId(index, side),
      width: 0,
      height: 0,
      layoutOptions: {
        "org.eclipse.elk.port.side": SIDES[side],
      },
    })),
  }));
  const orderedEdges = [...page.edges].sort((a, b) => stableCompare(a.edge_id, b.edge_id));
  const edges = orderedEdges.map((edge, index) => {
    if (!nodeIndex.has(edge.source) || !nodeIndex.has(edge.target)) {
      throw new Error(`edge ${edge.edge_id} references an unknown node`);
    }
    const result = {
      id: `e${index}`,
      sources: [portId(nodeIndex.get(edge.source), edge.source_port)],
      targets: [portId(nodeIndex.get(edge.target), edge.target_port)],
    };
    if (edge.label_size) {
      result.labels = [{
        id: `e${index}-label`,
        width: edge.label_size.width,
        height: edge.label_size.height,
      }];
    }
    return result;
  });
  return {
    graph: {
      id: `page-${page.page_id}`,
      layoutOptions: effectiveOptions(input),
      children,
      edges,
    },
    orderedNodes,
    orderedEdges,
    nodeIndex,
  };
}

function finite(value, description) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${description} is not finite`);
  }
  return value;
}

function point(value, description) {
  return {
    x: finite(value?.x, `${description}.x`),
    y: finite(value?.y, `${description}.y`),
  };
}

function pointsEqual(left, right) {
  return left.x === right.x && left.y === right.y;
}

function normalizePoints(points) {
  const deduplicated = [];
  for (const item of points) {
    if (!deduplicated.length || !pointsEqual(deduplicated[deduplicated.length - 1], item)) {
      deduplicated.push(item);
    }
  }
  if (deduplicated.length < 2) {
    throw new Error("ELK edge route contains fewer than two distinct points");
  }
  for (let index = 1; index < deduplicated.length; index += 1) {
    const before = deduplicated[index - 1];
    const after = deduplicated[index];
    if (before.x !== after.x && before.y !== after.y) {
      throw new Error("ELK edge route contains a diagonal segment");
    }
  }
  return deduplicated;
}

function joinSections(sections, edgeId) {
  if (!Array.isArray(sections) || sections.length === 0) {
    throw new Error(`ELK edge ${edgeId} has no routed sections`);
  }
  const remaining = sections.map((section, index) => ({
    id: String(section.id ?? index),
    points: [
      point(section.startPoint, `edge ${edgeId} section start`),
      ...(Array.isArray(section.bendPoints)
        ? section.bendPoints.map((item, itemIndex) => point(item, `edge ${edgeId} bend ${itemIndex}`))
        : []),
      point(section.endPoint, `edge ${edgeId} section end`),
    ],
  })).sort((left, right) => stableCompare(left.id, right.id));
  const route = remaining.shift().points;
  while (remaining.length) {
    const tail = route[route.length - 1];
    const nextIndex = remaining.findIndex((candidate) => pointsEqual(candidate.points[0], tail));
    if (nextIndex < 0) {
      throw new Error(`ELK edge ${edgeId} sections are disconnected`);
    }
    const next = remaining.splice(nextIndex, 1)[0].points;
    route.push(...next.slice(1));
  }
  return normalizePoints(route);
}

function boundaryPoint(bounds, side, rawPoint) {
  const x = finite(bounds.x, "node.x");
  const y = finite(bounds.y, "node.y");
  const width = finite(bounds.width, "node.width");
  const height = finite(bounds.height, "node.height");
  if (side === "north" || side === "south") {
    return {
      point: { x: rawPoint.x, y: side === "north" ? y : y + height },
      pin: (rawPoint.x - x) / width,
    };
  }
  return {
    point: { x: side === "west" ? x : x + width, y: rawPoint.y },
    pin: (rawPoint.y - y) / height,
  };
}

function strictPin(value, edgeId, endpoint) {
  if (!Number.isFinite(value) || value < 0.1 || value > 0.9) {
    throw new Error(`ELK edge ${edgeId} ${endpoint} pin is outside the supported range`);
  }
  return value;
}

function routeSegments(points) {
  return points.slice(1).map((end, index) => ({ start: points[index], end }));
}

function segmentLength(segment) {
  return Math.abs(segment.end.x - segment.start.x) + Math.abs(segment.end.y - segment.start.y);
}

function collinearOverlap(left, right) {
  if (left.start.x === left.end.x && right.start.x === right.end.x && left.start.x === right.start.x) {
    return Math.max(0, Math.min(Math.max(left.start.y, left.end.y), Math.max(right.start.y, right.end.y))
      - Math.max(Math.min(left.start.y, left.end.y), Math.min(right.start.y, right.end.y)));
  }
  if (left.start.y === left.end.y && right.start.y === right.end.y && left.start.y === right.start.y) {
    return Math.max(0, Math.min(Math.max(left.start.x, left.end.x), Math.max(right.start.x, right.end.x))
      - Math.max(Math.min(left.start.x, left.end.x), Math.min(right.start.x, right.end.x)));
  }
  return 0;
}

function strictInteriorCrossing(left, right) {
  const leftVertical = left.start.x === left.end.x;
  const rightVertical = right.start.x === right.end.x;
  if (leftVertical === rightVertical) {
    return false;
  }
  const vertical = leftVertical ? left : right;
  const horizontal = leftVertical ? right : left;
  const x = vertical.start.x;
  const y = horizontal.start.y;
  return x > Math.min(horizontal.start.x, horizontal.end.x)
    && x < Math.max(horizontal.start.x, horizontal.end.x)
    && y > Math.min(vertical.start.y, vertical.end.y)
    && y < Math.max(vertical.start.y, vertical.end.y);
}

function rectOverlap(left, right) {
  return left.x < right.x + right.width
    && left.x + left.width > right.x
    && left.y < right.y + right.height
    && left.y + left.height > right.y;
}

function metrics(pages) {
  const allSegments = [];
  const allNodes = [];
  const allLabels = [];
  let routeLength = 0;
  let bendCount = 0;
  for (const page of pages) {
    allNodes.push(...page.nodes.map((node) => ({ page: page.page_id, ...node })));
    for (const edge of page.edges) {
      const segments = routeSegments(edge.waypoints);
      routeLength += segments.reduce((sum, segment) => sum + segmentLength(segment), 0);
      bendCount += Math.max(0, edge.waypoints.length - 2);
      allSegments.push(...segments.map((segment) => ({ page: page.page_id, edge: edge.edge_id, ...segment })));
      if (edge.label_bounds) {
        allLabels.push({ page: page.page_id, ...edge.label_bounds });
      }
    }
  }
  let crossings = 0;
  let sharedRouteLength = 0;
  for (let left = 0; left < allSegments.length; left += 1) {
    for (let right = left + 1; right < allSegments.length; right += 1) {
      if (allSegments[left].page !== allSegments[right].page || allSegments[left].edge === allSegments[right].edge) {
        continue;
      }
      if (strictInteriorCrossing(allSegments[left], allSegments[right])) {
        crossings += 1;
      }
      sharedRouteLength += collinearOverlap(allSegments[left], allSegments[right]);
    }
  }
  let overlaps = 0;
  for (let left = 0; left < allNodes.length; left += 1) {
    for (let right = left + 1; right < allNodes.length; right += 1) {
      if (allNodes[left].page === allNodes[right].page && rectOverlap(allNodes[left], allNodes[right])) {
        overlaps += 1;
      }
    }
  }
  let labelCollisions = 0;
  for (const label of allLabels) {
    labelCollisions += allNodes.filter((node) => node.page === label.page && rectOverlap(label, node)).length;
  }
  return {
    crossings,
    overlaps,
    route_length: routeLength,
    bend_count: bendCount,
    shared_route_length: sharedRouteLength,
    label_collisions: labelCollisions,
  };
}

function fromElkPage(page, input, converted, output) {
  const outputNodes = new Map((output.children ?? []).map((node) => [node.id, node]));
  const nodes = converted.orderedNodes.map((source, index) => {
    const laidOut = outputNodes.get(`n${index}`);
    if (!laidOut) {
      throw new Error(`ELK omitted node ${source.node_id}`);
    }
    return {
      node_id: source.node_id,
      x: finite(laidOut.x, `node ${source.node_id}.x`),
      y: finite(laidOut.y, `node ${source.node_id}.y`),
      width: finite(laidOut.width, `node ${source.node_id}.width`),
      height: finite(laidOut.height, `node ${source.node_id}.height`),
      locked: Boolean(source.locked),
    };
  });
  const bounds = new Map(nodes.map((node) => [node.node_id, node]));
  const outputEdges = new Map((output.edges ?? []).map((edge) => [edge.id, edge]));
  const edges = converted.orderedEdges.map((source, index) => {
    const laidOut = outputEdges.get(`e${index}`);
    if (!laidOut) {
      throw new Error(`ELK omitted edge ${source.edge_id}`);
    }
    const waypoints = joinSections(laidOut.sections, source.edge_id);
    const sourceBoundary = boundaryPoint(bounds.get(source.source), source.source_port, waypoints[0]);
    const targetBoundary = boundaryPoint(bounds.get(source.target), source.target_port, waypoints[waypoints.length - 1]);
    waypoints[0] = sourceBoundary.point;
    waypoints[waypoints.length - 1] = targetBoundary.point;
    const normalized = normalizePoints(waypoints);
    const edge = {
      edge_id: source.edge_id,
      source: source.source,
      target: source.target,
      edge_class: source.edge_class,
      source_port: source.source_port,
      target_port: source.target_port,
      source_pin: strictPin(sourceBoundary.pin, source.edge_id, "source"),
      target_pin: strictPin(targetBoundary.pin, source.edge_id, "target"),
      waypoints: normalized,
    };
    const label = Array.isArray(laidOut.labels) ? laidOut.labels[0] : null;
    if (source.label_size && label) {
      edge.label_bounds = {
        x: finite(label.x, `edge ${source.edge_id} label.x`),
        y: finite(label.y, `edge ${source.edge_id} label.y`),
        width: finite(label.width, `edge ${source.edge_id} label.width`),
        height: finite(label.height, `edge ${source.edge_id} label.height`),
      };
    }
    return edge;
  });
  const channelReservations = [];
  for (const edge of edges) {
    for (const segment of routeSegments(edge.waypoints)) {
      channelReservations.push({
        edge_id: edge.edge_id,
        start: segment.start,
        end: segment.end,
      });
    }
  }
  return {
    page_id: page.page_id,
    name: String(page.name ?? ""),
    nodes,
    edges,
    channel_reservations: channelReservations,
  };
}

async function run(input) {
  if (!input || input.schema_version !== 1 || !Array.isArray(input.pages) || input.pages.length === 0) {
    throw new Error("expected layout-request.v1 JSON on stdin");
  }
  const elk = new ELK();
  const pages = [];
  for (const page of [...input.pages].sort((a, b) => stableCompare(a.page_id, b.page_id))) {
    const converted = toElkGraph(page, input);
    const output = await elk.layout(converted.graph, {
      layoutOptions: effectiveOptions(input),
    });
    pages.push(fromElkPage(page, input, converted, output));
  }
  const requestSha256 = input.__request_sha256;
  if (typeof requestSha256 !== "string" || !/^[a-f0-9]{64}$/.test(requestSha256)) {
    throw new Error("host request digest is missing");
  }
  return {
    schema_version: 1,
    result_id: `elk-${requestSha256.slice(0, 16)}`,
    request_sha256: requestSha256,
    backend: BACKEND_ID,
    pages,
    metrics: metrics(pages),
  };
}

async function main() {
  if (process.argv.slice(2).includes("--probe")) {
    process.stdout.write(JSON.stringify({
      bridge: "drawio-elk-runner",
      elkjs_version: ELKJS_VERSION,
      backend: BACKEND_ID,
    }));
    return;
  }
  const input = JSON.parse(await readStdin());
  process.stdout.write(JSON.stringify(await run(input)));
}

main().catch((error) => {
  process.stderr.write(`${error?.stack ?? error}\n`);
  process.exitCode = 2;
});
