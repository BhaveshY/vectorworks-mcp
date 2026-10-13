# Codex end-to-end test prompt (Vectorworks MCP 1.0)

Paste the prompt below into a **new Codex thread** (Codex desktop or `codex` CLI) with
Vectorworks 2024/2025 running. It touches only documents it creates and closes them
without saving. Run it once with your normal model and once with a small model
(e.g. `gpt-5.6-luna`) to check weak-model behaviour.

Expected result: every step PASS, no shell/computer-use, no "connector unavailable",
roughly 40–70 tool calls, 5–15 minutes.

---

```text
You are testing the Vectorworks MCP connector end to end. Use ONLY the vectorworks MCP tools
(vw_status, vw_read, vw_capture, vw_create, vw_edit, vw_apply, vw_view, vw_organize,
vw_document, vw_script). Do not use shell commands, computer use, GUI automation or files on disk,
except writing the exports named below into %TEMP%\vw-e2e.

Safety: never modify, save or close a document you did not create in this test. Create new
blank documents with vw_document(action="new") and work only in those. At the end close every
document you created WITHOUT saving (discard changes).

Follow error.next_step whenever a call fails; never repeat an identical failing call. After every
step write one line "STEP n: PASS|FAIL - <evidence>" where the evidence comes from a tool result
or vw_read readback (never from memory). If a step fails, continue with the next one.

STEP 1 - Connection: vw_status(). Report Vectorworks version, bridge version, ui_state and the
open documents. (If Vectorworks is not running, report the next_step text and stop.)

STEP 2 - Document A: create a new blank document A. Confirm with vw_read(what="documents") that
it is active and untitled.

STEP 3 - Floor plan (one vw_create call): an 10 m x 7 m two-room plan, 200 mm exterior walls
2700 mm high, a 100 mm interior wall splitting it into a 6 m and a 4 m room, a 900 mm door in
the south wall of each room, a 1000 mm door in the interior wall, two 1500 mm windows in the north
wall, and Spaces named "Living" and "Bedroom". Read back counts of walls, doors, windows and
spaces with vw_read(what="query", type=...). Expected: 5 walls, 3 doors, 2 windows, 2 spaces.

STEP 4 - Visual check: vw_capture the drawing in top/plan view. Describe what you see (rooms,
door swings, windows, labels) and say whether the wall corners are joined cleanly.

STEP 5 - Edit: move the interior wall 500 mm east, make the Bedroom window 1800 mm wide, and
rename the Space "Bedroom" to "Office" - all in as few calls as possible. Read back the new values.

STEP 6 - Organisation: create design layers "Ground" (elevation 0) and "First" (elevation 3000 mm),
a class "Walls-Ext" with a red pen colour, assign the exterior walls to it, hide the class
"None" for a moment and then show it again. Read back the layer elevations and class settings.

STEP 7 - Graphics: on layer "First" draw three 3 x 2 m rectangles filled with a cross hatch, a
gradient and no fill (dashed outline on the third), and a bold 14 pt Arial text "SITE PLAN".
Capture them and describe the fills and line style you see.

STEP 8 - Massing: on layer "First" create an extrude 12 x 8 m, 9 m high, and four 400 mm square
columns at its corners. Capture a right isometric view with a shaded render mode and describe it.
Then capture top/plan again and confirm the user's view was restored (vw_read(what="view")
before and after should match).

STEP 9 - Sheets: create sheet layer "A-101" with a viewport of layer "Ground" at 1:50, top/plan,
titled "GROUND FLOOR 1:50", and a second viewport of layer "First" in right isometric at 1:200.
Read back both viewport scales with vw_read(what="viewports"). Capture the sheet and describe it.

STEP 10 - Export: export sheet "A-101" to PDF and the whole drawing to DWG in %TEMP%\vw-e2e.
Report file sizes from the tool results.

STEP 11 - Multi-document: create document B, draw a 2 m radius circle and a text "Plan B".
Switch back to document A explicitly (by document), rotate the extrude by 15 degrees, then read
object counts of BOTH documents. Confirm nothing from B appeared in A and vice versa.

STEP 12 - Scripting: in document A use vw_script (python) to count all walls and return the count
as `result`. Then run a script that creates a rectangle and then raises an error, and verify with
vw_read that the rectangle was rolled back.

STEP 13 - Recovery: call vw_read with an invalid argument on purpose (e.g. what="nonsense") and
report the error code and next_step you received (this step PASSES if the error is clear).

STEP 14 - Clean up: close documents A and B without saving. vw_status() must show the same
documents as in STEP 1.

FINAL: print a table of the 14 steps with PASS/FAIL, the total number of tool calls you made,
how many returned errors, and anything that confused you about the tools.
```
