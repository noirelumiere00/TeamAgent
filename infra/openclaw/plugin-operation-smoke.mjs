#!/usr/bin/env node

// Representative production-operation module loads.  All provider clients are
// injected stubs and the containing docker run uses --network none.  This
// catches pruning of lazy/dynamic/bare imports used by actual Slack read/write
// and Bedrock discovery paths without contacting a provider.

const slack = await import("/opt/teamagent/plugins/slack/dist/api.js");
const slackCalls = [];
const slackClient = {
  conversations: {
    history: async (input) => {
      slackCalls.push({ operation: "conversations.history", input });
      return {
        messages: [{ ts: "1.200000", text: "stubbed Slack history" }],
        has_more: false,
      };
    },
  },
  chat: {
    update: async (input) => {
      slackCalls.push({ operation: "chat.update", input });
      return { ok: true, ts: input.ts };
    },
  },
};
const slackRead = await slack.readSlackMessages("C0123456789", {
  client: slackClient,
  limit: 1,
});
await slack.editSlackMessage(
  "C0123456789",
  "1.200000",
  "stubbed Slack edit",
  { client: slackClient },
);
if (
  slackRead.messages.length !== 1 ||
  slackRead.messages[0].text !== "stubbed Slack history" ||
  slackCalls.map((entry) => entry.operation).join(",") !==
    "conversations.history,chat.update"
) {
  throw new Error("representative Slack operation smoke returned an unexpected result");
}

const bedrock = await import(
  "/opt/teamagent/plugins/amazon-bedrock/dist/api.js"
);
const bedrockCommands = [];
await bedrock.discoverBedrockModels({
  region: "ap-northeast-1",
  config: { refreshInterval: 0 },
  clientFactory: () => ({
    send: async (command) => {
      bedrockCommands.push(command.constructor.name);
      if (command.constructor.name === "ListFoundationModelsCommand") {
        return { modelSummaries: [] };
      }
      if (command.constructor.name === "ListInferenceProfilesCommand") {
        return { inferenceProfileSummaries: [] };
      }
      throw new Error(`unexpected Bedrock command: ${command.constructor.name}`);
    },
  }),
});
if (
  bedrockCommands.join(",") !==
  "ListFoundationModelsCommand,ListInferenceProfilesCommand"
) {
  throw new Error("representative Bedrock operation smoke did not load both operations");
}

process.stdout.write(
  `${JSON.stringify({
    schemaVersion: 1,
    network: "disabled-by-container",
    slack: {
      module: "/opt/teamagent/plugins/slack/dist/api.js",
      operations: slackCalls.map((entry) => entry.operation),
      providerCallsStubbed: true,
    },
    bedrock: {
      module: "/opt/teamagent/plugins/amazon-bedrock/dist/api.js",
      operations: bedrockCommands,
      providerCallsStubbed: true,
    },
    passed: true,
  })}\n`,
);
