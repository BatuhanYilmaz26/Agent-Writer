// Preserve browser preferences when upgrading an existing local workspace.
// The previous prefix is used only to read and retire legacy keys.
export function migratePreferences(storage) {
  for (const suffix of ["theme", "run"]) {
    const current = `fieldnote.${suffix}`;
    const previous = `agentwriter.${suffix}`;
    const value = storage.getItem(previous);
    if (value === null) continue;
    if (storage.getItem(current) === null) storage.setItem(current, value);
    // If writing fails, retain the old key so a later attempt can recover it.
    if (storage.getItem(current) !== null) storage.removeItem(previous);
  }
}
