/** Plain-language text for the parser and mapping messages the script check can return. */
export interface ScriptProblem { kind: string; line: number; statement: string; message: string }
export interface Explained { text: string; fix: string }

const where = (p: ScriptProblem) => (p.line > 0 ? ` (line ${p.line})` : "");

export function explain(p: ScriptProblem): Explained {
  const m = p.message;
  if (/^Nothing to import/i.test(m))
    return { text: "The file has nothing to import.",
             fix: "Check that you chose the right file. It needs CREATE TABLE statements or COPY / INSERT rows." };
  if (/not valid UTF-8/i.test(m))
    return { text: "The file is not UTF-8 text.", fix: "Save the file as UTF-8 and upload it again." };
  if (/NUL bytes/i.test(m))
    return { text: "The file contains binary data.", fix: "Upload the plain-text SQL file, not a compressed or custom-format dump." };
  if (/^not valid SQL/i.test(m))
    return { text: `Part of the file is not valid SQL${where(p)}.`,
             fix: `Look at line ${p.line || "?"} of the file. If you edited the dump by hand, undo that edit or export it again with pg_dump.` };
  if (/is not allowed in a script/i.test(m))
    return { text: `This statement would delete or change existing data${where(p)}.`,
             fix: "Remove it from the file. A script can only create tables and load rows." };
  if (/type .* is not supported/i.test(m))
    return { text: "A column type is not supported yet.",
             fix: "Change that column to a supported type (text, varchar, integer, bigint, numeric, boolean, date, timestamp, uuid, jsonb) and upload again." };
  if (/default .* is not supported/i.test(m))
    return { text: "A default value is not supported.", fix: "Remove the DEFAULT from that column, or use a constant, now() or CURRENT_DATE." };
  if (/is created twice/i.test(m))
    return { text: "The same table is created twice.", fix: "Keep each CREATE TABLE only once in the file and upload again." };
  if (/longer than 63 characters/i.test(m))
    return { text: "A name is too long.", fix: "Names can be at most 63 characters. Shorten it in the file." };
  if (/row has \d+ fields, expected \d+/i.test(m))
    return { text: `A data row has the wrong number of columns${where(p)}.`,
             fix: "Check that line of the COPY block: values must be separated by single tab characters." };
  if (/above the limit maxOpsPerScript/i.test(m))
    return { text: "The file has too many operations.",
             fix: "Split the file in parts, or ask an approver to raise maxOpsPerScript in Settings." };
  if (p.kind === "mapping")
    return { text: "The destination setting has a problem.",
             fix: "Open Customize (optional) and correct it, or clear it and the server suggests a destination." };
  return { text: "Something in the file could not be handled.", fix: m };
}
