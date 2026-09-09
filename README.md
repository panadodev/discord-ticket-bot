# Archipel [Ticket Bot Configuration](https://github.com/neoArchipel/ticket-bot/blob/main/config.json) Guide

## Environment Setup

Copy [.env.example](.env.example) to `.env` and fill in your bot token, config path, and database URI before starting the bot.

If you keep your real bot settings outside the repo, set `CONFIG_PATH` to that file. Otherwise the bot will load [`config.json`](config.json) from the repository root.

## Features

### FAQ System

The bot can automatically respond to frequently asked questions in guild channels. When a message matches any of the configured FAQ patterns, the bot will reply with the corresponding answer.

#### Configuration

Add FAQ entries in `config.json` under the `faq` section:

```json
"faq": {
    "questions": [
        {
            "question": "console server, 3x console",
            "answer": "We do not have any console servers."
        }
    ]
}
```

#### How It Works

- The bot monitors messages in **guild text channels only** (not DMs, not ticket channels, not main guild)
- FAQ replies will **only** be sent for messages ending with a question mark (`?`)
- FAQ replies will NOT be sent in the main guild (configured via `main_guild_id`)
- FAQ replies will NOT be sent if the message is itself a reply to another message
- Patterns are comma-separated in the `question` field
- Matching is **case-insensitive** and uses **whole-word matching**
- Patterns must match complete words/phrases (e.g., "console server" won't match in "testconsoletest")
- The bot will respond if the message contains **any** of the listed patterns

#### Examples

With the config above, the bot will respond to:

- "Are there console servers?" → ✓ matches "console server" (has ?)
- "Where is the 3x console?" → ✓ matches "3x console" (has ?)
- "Do you have a console server?" → ✓ matches "console server" (has ?)
- "console server" → ✗ no match (no question mark)
- "testconsoletest testservertest?" → ✗ no match (not whole words)

### Ticket System

[Additional ticket system documentation can be added here]

## tickets

Configure how each button will work, who can access them and where they are logged when closed etc

### ticket config example

| Field                        | Description                                                                                                             | Example                                                                                                                                               |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| "log_channel"                | Where the ticket transcript will be stored                                                                              | `1033378494702428271`                                                                                                                                 |
| "button_name"                | Name of the button                                                                                                      | `"Player Report"`                                                                                                                                     |
| "button_id"                  | Has to do with button persistance                                                                                       | `"player_report"`                                                                                                                                     |
| "button_style"               | Primary (blue), Secondary (grey), Success (green), Danger (red), and Link (which directs to a URL)                      | `"DANGER"`                                                                                                                                            |
| "ticket_channel_icon"        | Channel icon (Can't be any, discord **does not** have all emojis set up like that                                       | `"🟢"`                                                                                                                                                |
| "embed_color"                | Every ticket type has a color theme                                                                                     | `"#00ff00"`                                                                                                                                           |
| "has_perms"                  | Who can handle the ticket (Roles set up at top of config.json)                                                          | `["management","trial_admin","trainee_admin","admin","senior_admin"]`                                                                                 |
| "questions"                  | First question has to always be **_What is your steam id?_**                                                            | `["Please provide the steam id/s of the user/s you are reporting:","On which server are they:","Why are they being reported:"]`                       |
| "check_for_steamid_provided" | Does the bot need to check if they actually provided a steam id or not? Saves a lot of time for the admins...           | `true`                                                                                                                                                |
| "find_steam_id"              | The instructions on how to find a steam id (only sent if they could not provide and check_for_steamid_provided is true) | `"Press F7 in game and click on the *copy* button next to the steam id: \n https://pub-ac6368b6320d4e8bb06d39c4ace57205.r2.dev/findothersteamid.png`" |

Org ticket category is configured per organization under `orgs -> <org> -> tickets_cat`.
