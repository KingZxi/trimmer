"""The Help menu's pages: getting started and keyboard shortcuts (OBS tips live in settings_dialog)."""

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout

GETTING_STARTED = """
<h2>Getting started</h2>
<p>Trimmer never changes your original files: every result is saved as a new file, checked, and only
then can the original be deleted (if you ask for that). Your clips never leave your PC; ffmpeg on your own
PC does all the work. The only thing that ever connects is the optional update check, which only asks
GitHub for the latest version (Settings &gt; Updates).</p>

<h3>1. Trim a clip</h3>
<ol>
<li>Drop a clip on the window, or use <b>Add files</b>. It appears in the <b>Clips</b> tab.</li>
<li>Play it (<b>Space</b>), and press <b>Mark in</b> (<b>I</b>) where the part you want starts and
<b>Mark out</b> (<b>O</b>) where it ends. Mark more parts to cut out the bits between them.</li>
<li>Type a name in <b>Save as</b> if you like, then press <b>Add to queue</b>.</li>
<li>The <b>Queue</b> tab saves it. By default it lands next to the original; <b>Output folder</b>
in the toolbar changes that.</li>
</ol>
<p>Cuts normally start on the keyframe just before your mark, so nothing you marked is lost and the
video isn't re-encoded. Tick <b>Frame-exact cuts</b> to start exactly on your mark.</p>

<h3>2. Make a clip smaller (for Discord and the like)</h3>
<ol>
<li>Select the clip, tick <b>Compress</b> and pick a preset, e.g. <b>Discord 10 MB (720p)</b>.</li>
<li><b>Estimate size</b> shows roughly how big it will be. <b>Edit…</b> changes the settings.</li>
<li><b>Add to queue</b>. In <b>Edit…</b>, <b>Keep the original if compressing would make it bigger</b>
makes sure you never end up with a bigger file.</li>
</ol>

<h3>3. Join OBS Replay Buffer saves</h3>
<ol>
<li>Drop the replays (or their folder) on the <b>Replay merge</b> tab. Replays that overlap are
grouped into sessions.</li>
<li>Tick the sessions you want. Each becomes one video with no repeated or missing footage, or pick
<b>Keep separate, remove repeated footage</b> for separate clips.</li>
<li>Press <b>Add ticked sessions to queue</b>.</li>
</ol>
<p><b>Watch folder</b> adds new replays as OBS saves them. Don't use the Replay Buffer? Hide the tab in
Settings &gt; Replay merge.</p>

<h3>4. Audio tracks</h3>
<p>OBS can record your mic and game sound as separate tracks. Untick a track to leave it out, tick
<b>Mix kept tracks into one</b> so phones and Discord play them all, or use <b>Rip tracks to separate
files</b> to save each track as its own audio file.</p>

<h3>Good to know</h3>
<ul>
<li>Select several clips to change their options together. A clip with its own options is marked ★;
<b>Use shared options</b> puts it back.</li>
<li>Right-click a clip, session or job for more: show the file in its folder, split parts, move to the
Recycle Bin, retry, and so on.</li>
<li>The queue can pause itself while you play a full screen game, and has a global pause key
(Settings &gt; Queue).</li>
<li>Something went wrong? The Queue tab's <b>Logs</b> button opens the logs, and the job's details say
what happened.</li>
</ul>
"""

SHORTCUTS = """
<h2>Keyboard shortcuts</h2>
<h3>Anywhere</h3>
<table cellpadding="4">
<tr><td><b>Ctrl+O</b></td><td>Add files</td></tr>
<tr><td><b>Ctrl+Shift+O</b></td><td>Add a folder</td></tr>
<tr><td><b>Ctrl+,</b></td><td>Settings</td></tr>
<tr><td><b>F1</b></td><td>Getting started</td></tr>
<tr><td><b>Ctrl+Alt+P</b></td><td>Pause or resume the queue, even in a game (changeable in Settings &gt; Queue)</td></tr>
</table>
<h3>Clips tab</h3>
<table cellpadding="4">
<tr><td><b>Space</b></td><td>Play / pause</td></tr>
<tr><td><b>Left / Right</b></td><td>One frame back / forward</td></tr>
<tr><td><b>J / L</b></td><td>5 seconds back / forward</td></tr>
<tr><td><b>I / O</b></td><td>Mark in / mark out</td></tr>
<tr><td><b>S</b></td><td>Split the clip here</td></tr>
<tr><td><b>F2</b></td><td>Rename the output</td></tr>
<tr><td><b>F11</b></td><td>Full screen (Esc leaves)</td></tr>
<tr><td><b>Delete</b></td><td>Remove the selected clips from the list</td></tr>
<tr><td><b>Shift+Delete</b></td><td>Move the selected clips to the Recycle Bin</td></tr>
<tr><td><b>Esc</b> (in the list)</td><td>Deselect, back to the options for all clips</td></tr>
</table>
<h3>Replay merge tab</h3>
<table cellpadding="4">
<tr><td><b>F2</b></td><td>Name the selected session's output</td></tr>
<tr><td><b>Delete</b></td><td>Remove the selected replays from the list</td></tr>
<tr><td><b>Shift+Delete</b></td><td>Move the selected replays to the Recycle Bin</td></tr>
<tr><td><b>Shift-click a tick</b></td><td>Tick or untick every session</td></tr>
<tr><td><b>Esc</b> (in the list)</td><td>Deselect, back to the options for all sessions</td></tr>
</table>
<h3>Clip viewers</h3>
<table cellpadding="4">
<tr><td><b>Space, Left / Right, F11</b></td><td>As in the Clips tab</td></tr>
</table>
"""


class HelpDialog(QDialog):
    def __init__(self, title: str, html: str, parent=None, size=(620, 640)):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        lay = QVBoxLayout(self)
        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        self.text.setHtml(html)
        lay.addWidget(self.text)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
