# Scouter — Scout Note Generation Prompt

Write a 1-2 sentence scouting note for this player.
Scouting query: {query}
Player data from database:
{player_json}

Explain specifically why this player fits or doesn't fit the query.
Base it ONLY on the stats provided — do not invent any information.
Return JSON: {{"scout_note": "<1-2 sentences>"}}
