let jsonMode = false;

export function setJsonMode(val: boolean): void {
  jsonMode = val;
}

export function isJsonMode(): boolean {
  return jsonMode;
}

export function output(data: unknown, humanText: string): void {
  if (jsonMode) {
    console.log(JSON.stringify(data, null, 2));
  } else {
    console.log(humanText);
  }
}

export function handleError(err: unknown): never {
  if (jsonMode) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(JSON.stringify({ error: msg }));
  } else {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`Error: ${msg}`);
  }
  process.exit(1);
}
