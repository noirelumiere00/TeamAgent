// 送信前洗浄の純関数を実物の plugin から呼ぶ。外部接続・socket は使わない。
import { readFileSync } from "node:fs";
import {
  OUTBOUND_TOOL_NAMES,
  scrubOutboundReplyText,
} from "../../infra/openclaw/caller-identity-plugin/dist/index.js";

const input = JSON.parse(readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify({
  toolNames: OUTBOUND_TOOL_NAMES,
  results: input.texts.map((text) => scrubOutboundReplyText(text)),
}) + "\n");
