export const FIELD_TYPES = Object.freeze(["string", "textarea", "number", "integer", "enum", "boolean"]);
export const ARTIFACT_TYPES = Object.freeze(["md", "docx", "pdf", "pptx"]);

export function defaultBuilderSkill(now = Date.now()) {
  return {
    skillId: `skill_custom_${now.toString(36)}`,
    name: "My Custom Skill",
    description: "A reusable custom Skill.",
    version: "1.0.0",
    systemPrompt: "You are a focused Skill. Follow the input schema, use only allowed tools, and return concise markdown.",
    inputSchema: {
      type: "object",
      properties: {
        topic: {
          type: "string",
          title: "Topic",
          description: "What should this Skill work on?",
          maxLength: 500,
        },
      },
      required: ["topic"],
      additionalProperties: false,
    },
    outputSchema: defaultOutputSchema("content"),
    allowedTools: ["search_files"],
    memoryPolicy: { scope: "project", read: true, write: false },
    artifactPolicy: { autoSave: true, types: ["md"] },
    projectBinding: { enabled: true },
    exampleInputs: [{ topic: "Example topic" }],
  };
}

export function fieldTypeFromSchema(prop = {}) {
  if (Array.isArray(prop.enum)) return "enum";
  if (prop.type === "string" && Number(prop.maxLength || 0) > 120) return "textarea";
  if (FIELD_TYPES.includes(prop.type)) return prop.type;
  return "string";
}

export function outputModeFromSchema(schema = {}) {
  return schema.properties?.title ? "title_content" : "content";
}

export function defaultOutputSchema(mode) {
  const properties = {
    content: { type: "string" },
    mode: { type: "string" },
  };
  const required = ["content"];
  if (mode === "title_content") {
    properties.title = { type: "string" };
    required.unshift("title");
  }
  return { type: "object", properties, required, additionalProperties: true };
}

export function buildInputSchema(fields) {
  const properties = {};
  const required = [];
  for (const field of fields) {
    properties[field.key] = builderFieldToSchema(field);
    if (field.required) required.push(field.key);
  }
  return { type: "object", properties, required, additionalProperties: false };
}

export function builderFieldToSchema(field) {
  const prop = {
    type: field.type === "textarea" || field.type === "enum" ? "string" : field.type,
    title: field.title || field.key,
  };
  if (field.description) prop.description = field.description;
  if (field.type === "enum") prop.enum = field.enumOptions.length ? field.enumOptions : ["option"];
  if (field.type === "textarea") prop.maxLength = field.maxLength || 2000;
  if (field.maxLength && (field.type === "string" || field.type === "textarea")) prop.maxLength = field.maxLength;
  const defaultValue = parseBuilderDefault(field);
  if (defaultValue !== undefined) prop.default = defaultValue;
  return prop;
}

export function parseBuilderDefault(field) {
  const value = field.defaultValue;
  if (value === "") return undefined;
  if (field.type === "number") return Number(value);
  if (field.type === "integer") return parseInt(value, 10);
  if (field.type === "boolean") return value === "true" || value === "1" || String(value).toLowerCase() === "yes";
  return value;
}

export function sampleInputFromFields(fields) {
  const input = {};
  for (const field of fields) {
    if (!field.required && field.defaultValue === "") continue;
    const defaultValue = parseBuilderDefault(field);
    if (defaultValue !== undefined) input[field.key] = defaultValue;
    else if (field.type === "boolean") input[field.key] = true;
    else if (field.type === "number") input[field.key] = 1.5;
    else if (field.type === "integer") input[field.key] = 1;
    else if (field.type === "enum") input[field.key] = field.enumOptions[0] || "option";
    else input[field.key] = `Sample ${field.title || field.key}`;
  }
  return input;
}
