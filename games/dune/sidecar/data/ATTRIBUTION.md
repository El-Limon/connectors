# Item and entity catalogue — where the names come from

Takaro shows an item's **name** in the item picker, in `itemSearch`, in every shop listing and in every module
message. Dune: Awakening's item *template ids* (`ScrapMetal`, `T3UniqueComponent`) are what the game accepts, but
they are not names a player reads, and they do not appear in the shipped Linux server binary at all — they live in
the cooked content. So the human names have to come from somewhere else.

## What ships in this repository

| File | Contents | Rights |
|---|---|---|
| `items.json` | **Placeholder.** 15 rows — the emotes, CHOAM social cosmetics and the Solari currency stack — hand-written by us from template ids observed in a live battlegroup database. | Ours, same licence as this repository. |
| `entities.json` | 1 row (Sandworm), hand-named by us from the server binary's C++ RTTI. | Ours, same licence as this repository. |
| `scripts/gen-catalogue.mjs` | The generator that builds the full catalogue. | Ours. |

The full ~2200-row catalogue is **not** redistributed here.

## Generating the full catalogue

The generator fetches the [Dune: Awakening Community Wiki](https://awakening.wiki) public read-only API
(<https://api.awakening.wiki/items>), whose `item_id` field is the game's item template id, and keeps only rows that
have both a usable code and a genuinely human name (it drops asset-id-looking names rather than passing them on to
Takaro).

```bash
cd sidecar
npm run catalogue        # writes data/items.json, data/entities.json and data/SOURCES.md
```

The sidecar's Docker image runs this during the build, and its entrypoint runs it on first start if `items.json` is
still the placeholder. Both fall back to the placeholder if the wiki is unreachable — the connector then resolves
inventory rows by code instead of by name, and everything else keeps working.

## Licence of the generated data

> Item names and descriptions from the **Dune: Awakening Community Wiki** (<https://awakening.wiki>), licensed
> **CC BY-NC-SA 4.0** — Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International
> (<https://creativecommons.org/licenses/by-nc-sa/4.0/>).
>
> Dune: Awakening game content and materials © Funcom Oslo AS.

CC BY-NC-SA 4.0 is **non-commercial** and **share-alike**. A server operator generating this catalogue for their own
server is squarely within it. Redistributing the dataset inside a connector build is not obviously within it, which
is why this repository ships the generator and not the data. The generated JSON carries `source`, `sourceUrl`,
`license`, `licenseUrl` and `generatedAt`, `SOURCES.md` is written beside it, and the sidecar echoes `itemsSource` on
`/health`, so the attribution travels with the data wherever it ends up.

The connector's test suite deliberately does **not** read these files — it uses committed fixtures under
`src/testing/fixtures/` — so nothing in the build depends on who last ran the generator.
