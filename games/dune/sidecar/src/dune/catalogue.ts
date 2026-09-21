import fs from 'node:fs';
import { logger } from '../logger.js';
import { mapEntity, mapItemDefinition } from './mapping.js';
import type { TakaroEntity, TakaroItem } from './types.js';

/**
 * The item / entity catalogue.
 *
 * `code` is the stable dev identifier (`items.template_id` for an item, the `DT_VehicleTemplates` row key or NPC class
 * for an entity) — it has to be, because that is what `AddItemToInventory.ItemName` and `SpawnVehicleAt.ClassName`
 * take. `name` MUST be the player-facing display name: a catalogue that ships `BP_Zombie_C` as a name reaches Takaro's
 * item search, the give-item picker and every shop listing, and has burned a previous campaign
 * (memory: catalogue-human-names).
 *
 * The real dataset is produced by our own generator (`scripts/gen-catalogue.mjs`, a later lane) from the server's
 * datatables plus a display-name source. Until it exists this ships a small hand-written placeholder so the wire shape
 * and the aggregation logic are testable, and `listItems` is honestly reported as untested in the README.
 */
export interface CatalogueFile<T> {
  /** Where the rows came from, echoed on `/health` so an operator can tell a generated catalogue from the stub. */
  source?: string;
  generatedAt?: string;
  rows: T[];
}

export interface CatalogueOptions {
  itemsFile: string;
  entitiesFile: string;
}

/**
 * Item codes that are not possessions and must never be offered as giveable or reported as carried inventory.
 *
 * `Emote_*` are unlockable animations the game happens to model as rows in `inventories`. They cannot be dropped,
 * traded or meaningfully granted, and Tester saw ten of them in Takaro's inventory screen for his character. They are
 * filtered by CODE as well as by `inventory_type`, because the two defences fail differently: a future build may move
 * emotes to another container, and `DUNE_INVENTORY_TYPES` is operator-settable.
 *
 * They stay RESOLVABLE — `displayName()` still answers for them, so anything that does surface one (a log line, a
 * debug dump, a future build) shows "Emote: Bow" and not `Emote_Bow_01`.
 */
export const NON_CARRYABLE_CODE_RE = /^Emote_/i;

/** Catalogue categories that are resolvable for display but excluded from `listItems`. */
export const NON_GIVEABLE_CATEGORIES = new Set(['emote', 'contract', 'quest', 'cosmetic']);

/** A catalogue row the generator marked as not meaningfully giveable (emote, contract/quest item, cosmetic). */
export function isGiveable(row: { code: string; giveable?: boolean; category?: string }): boolean {
  if (row.giveable === false) return false;
  if (row.category && NON_GIVEABLE_CATEGORIES.has(row.category.toLowerCase())) return false;
  return !NON_CARRYABLE_CODE_RE.test(row.code);
}

export class Catalogue {
  /** Only the giveable items; this is what `listItems` answers. */
  private items: TakaroItem[] = [];
  private entities: TakaroEntity[] = [];
  /** EVERY row, giveable or not — the name-resolution index for inventory display. */
  private byCode = new Map<string, TakaroItem>();
  private itemsSource = 'none';
  private entitiesSource = 'none';
  private excluded = 0;

  constructor(private readonly options: CatalogueOptions) {}

  load(): void {
    const items = readCatalogue(this.options.itemsFile);
    this.items = [];
    this.byCode = new Map();
    this.excluded = 0;
    for (const raw of items.rows) {
      try {
        const item = mapItemDefinition(raw);
        // Every row is resolvable by code (so inventory display always has a human name), but only giveable rows
        // reach `listItems` — Takaro's item picker, every shop listing and every module `giveItem`.
        this.byCode.set(item.code.toLowerCase(), item);
        const meta = (raw ?? {}) as { giveable?: boolean; category?: string };
        if (isGiveable({ code: item.code, giveable: meta.giveable, category: meta.category })) this.items.push(item);
        else this.excluded += 1;
      } catch (err) {
        logger.warn(`Skipping catalogue item: ${(err as Error).message}`);
      }
    }
    this.itemsSource = items.source ?? (this.items.length ? 'file' : 'none');

    const entities = readCatalogue(this.options.entitiesFile);
    this.entities = [];
    for (const raw of entities.rows) {
      try {
        this.entities.push(mapEntity(raw));
      } catch (err) {
        logger.warn(`Skipping catalogue entity: ${(err as Error).message}`);
      }
    }
    this.entitiesSource = entities.source ?? (this.entities.length ? 'file' : 'none');
    logger.info(`Catalogue loaded: ${this.items.length} item(s) [${this.itemsSource}], ${this.entities.length} entity/entities [${this.entitiesSource}]`);
  }

  listItems(search?: string): TakaroItem[] {
    if (!search) return this.items.map((i) => ({ ...i }));
    const needle = search.toLowerCase();
    return this.items.filter((i) => i.code.toLowerCase().includes(needle) || i.name.toLowerCase().includes(needle)).map((i) => ({ ...i }));
  }

  listEntities(): TakaroEntity[] {
    return this.entities.map((e) => ({ ...e }));
  }

  /** Display name for an item code, for the inventory mapper. Undefined leaves the code as the name. */
  displayName(code: string): string | undefined {
    return this.byCode.get(code.toLowerCase())?.name;
  }

  /**
   * Display name for an ENTITY code, for the plugin's `entity-killed`.
   *
   * The plugin can only ever report a UE class name (`DuneNpcCharacter`) or a data-table row name
   * (`T3_Band_Slv_Reg_Marksman`) — this server build ships no `Content/Localization/`. Catalogue rule
   * (memory: catalogue-human-names): a dev name must never reach Takaro as a display name, so the
   * event mapper asks here and drops the row when the answer is undefined. Matching is
   * case-insensitive and also tries the name with a leading UE class prefix stripped, because the
   * plugin reports `DuneCritterBase` where a curated catalogue row may be keyed `ADuneCritterBase`.
   */
  entityName(code: string): string | undefined {
    if (!code) return undefined;
    const needle = code.toLowerCase();
    const stripped = needle.replace(/^[auefs](?=[a-z])/, '');
    for (const e of this.entities) {
      const key = e.code.toLowerCase();
      if (key === needle || key === stripped || key.replace(/^[auefs](?=[a-z])/, '') === needle) {
        // A catalogue row whose `name` IS its code is not a display name either.
        return e.name && e.name.toLowerCase() !== key ? e.name : undefined;
      }
    }
    return undefined;
  }

  /** Case-insensitive lookup; `AddItemToInventory.ItemName` is documented as case-insensitive too. */
  resolveCode(code: string): string | undefined {
    return this.byCode.get(code.toLowerCase())?.code;
  }

  status(): Record<string, unknown> {
    return {
      items: this.items.length,
      /** Rows resolvable for display but withheld from listItems (emotes, contract/quest items, cosmetics). */
      itemsNotGiveable: this.excluded,
      itemsResolvable: this.byCode.size,
      itemsSource: this.itemsSource,
      entities: this.entities.length,
      entitiesSource: this.entitiesSource,
    };
  }
}

function readCatalogue(file: string): CatalogueFile<unknown> {
  if (!file) return { rows: [] };
  let raw: string;
  try {
    raw = fs.readFileSync(file, 'utf8');
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
      logger.warn(`Catalogue file ${file} does not exist; that list will be empty until the generator has run`);
      return { rows: [] };
    }
    throw new Error(`Cannot read catalogue ${file}: ${(err as Error).message}`);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new Error(`Catalogue ${file} is not valid JSON: ${(err as Error).message}`);
  }
  if (Array.isArray(parsed)) return { rows: parsed };
  const rec = parsed as CatalogueFile<unknown>;
  return { rows: Array.isArray(rec?.rows) ? rec.rows : [], source: rec?.source, generatedAt: rec?.generatedAt };
}
