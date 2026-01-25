# Installing Computer Link Filter in OpenWebUI

## Quick Installation

### 1. Open OpenWebUI
Go to **Workspace** → **Functions**

### 2. Create a New Function
Click the **"+ Add Function"** or **"Import Function"** button

### 3. Copy the Code
Open the `computer_link_filter.py` file and copy all the code

### 4. Paste into the Editor
Paste the code into the OpenWebUI function editor

### 5. Save
Click **"Save"** to save the function

### 6. Enable the Function

#### Globally (for all models):
1. In the function list, find **"Computer Use Filter"**
2. Toggle the switch on the right to **ON**

#### For a specific model:
1. Go to **Workspace** → **Models**
2. Select the desired model
3. Open **Settings** → **Functions**
4. Enable **"Computer Use Filter"**

## Configuration (optional)

After enabling, you can customize the settings:

1. Click on **⚙️ (Settings)** next to the function
2. Modify **Valves**:
   - `FILE_SERVER_URL` - file server address (default: http://localhost:8081)
   - `ENABLE_ARCHIVE_BUTTON` - disable archive button
   - `ARCHIVE_BUTTON_TEXT` - change button text
   - `INJECT_SYSTEM_PROMPT` - disable prompt injection

## Verify It's Working

1. Create a new chat with a model that has the function enabled
2. Ask the AI to create a file:
   ```
   Create a test Excel file and save it
   ```
3. AI will **immediately** return a working link like `http://localhost:8081/files/{chat_id}/file.xlsx`
4. At the end of the message, a button **"📦 Download all files as archive"** will appear

## Requirements

- ✅ OpenWebUI >= 0.5.17
- ✅ File-server running and accessible
- ✅ Computer Use Tools installed with ID `ai_computer_use`

## Troubleshooting

### AI generates incorrect links?
- Check that the function is **enabled** (green toggle)
- Make sure `INJECT_SYSTEM_PROMPT = true` in Valves
- Restart the chat (system prompt applies at the start)

### Archive button doesn't appear?
- Make sure `ENABLE_ARCHIVE_BUTTON = true` in Valves
- Check that AI generated at least one file link

### File-server unavailable?
- Check that the server is running
- Check the URL in function Valves

## Uninstallation

1. Open **Workspace** → **Functions**
2. Find **"Computer Use Filter"**
3. Click **Delete** (trash icon)
4. Confirm deletion
