#!/usr/bin/env python3
"""
Aria2 Multi-task Downloader (Tkinter GUI)
-----------------------------------
Features:
- Download multiple tasks simultaneously, each task runs in an independent process.
- Support start, pause, resume, and stop operations.
- Show real-time download progress, speed, and ETA.
- Support various aria2 option configurations.
- Display task logs.
- English user interface.

Usage instructions:
1. Make sure aria2c is installed on your system or specify the aria2c path.
2. Enter the download link, configure the parameters, and then add the task.
3. Select a task from the task list to perform operations.
4. Pausing will terminate the process but keep the file, and resuming allows you to continue downloading from where you left off.
"""

import os
import sys
import threading
import subprocess
import shlex
import time
import re
import json
from queue import Queue, Empty
from tkinter import *
from tkinter import ttk, filedialog, messagebox
import tkinter as tk

APP_TITLE = "Aria2 Multi-task Downloader"
DEFAULT_ARIA2 = "aria2c"

PROGRESS_RE = re.compile(
    r'\[#([0-9a-f]+)\s+([0-9\.\w]+)\/([0-9\.\w]+)\(([0-9]+)%\)\s+CN:([0-9]+)\s+DL:([0-9\.\w]+)\s+ETA:([^\]]+)\]', 
    re.I
)

UNIT_RE = re.compile(r'([0-9\.]+)\s*([KMGT]?i?B)?', re.I)

def human_to_bytes(s):
    if not s: 
        return 0
    m = UNIT_RE.search(s)
    if not m: 
        try: 
            return int(s)
        except: 
            return 0
    val = float(m.group(1))
    unit = (m.group(2) or '').upper()
    if 'K' in unit: 
        return int(val * 1024)
    if 'M' in unit: 
        return int(val * 1024**2)
    if 'G' in unit: 
        return int(val * 1024**3)
    if 'T' in unit: 
        return int(val * 1024**4)
    return int(val)

class DownloadTask:
    def __init__(self, url, out_dir, out_name, options):
        self.url = url.strip()
        self.out_dir = out_dir or os.getcwd()
        self.out_name = out_name or ""
        self.options = options.copy()
        self.process = None
        self.gid = None
        self.progress = 0
        self.have_bytes = 0
        self.total_bytes = 0
        self.dl_speed = ""
        self.eta = ""
        self.connections = 0
        self.status = "Waiting..."
        self.log_lines = []
        self.stdout_thread = None
        self._stop_flag = threading.Event()

    def build_command(self, aria2_path):
        cmd = [aria2_path]
        
        if not os.path.isdir(self.out_dir):
            os.makedirs(self.out_dir, exist_ok=True)
            
        if self.options.get('continue', True):
            cmd.append('-c')  
            
        allocation = self.options.get('file-allocation', 'none')
        cmd.append(f'--file-allocation={allocation}')
        
        split = int(self.options.get('split', 4))
        max_conn = int(self.options.get('max-connection-per-server', split))
        cmd += [
            f'--split={split}',
            f'--max-connection-per-server={max_conn}'
        ]
        
        if 'max-tries' in self.options:
            cmd.append(f'--max-tries={self.options["max-tries"]}')
        if 'retry-wait' in self.options:
            cmd.append(f'--retry-wait={self.options["retry-wait"]}')
            
        if self.options.get('max-download-limit'):
            cmd.append(f'--max-download-limit={self.options["max-download-limit"]}')
        if self.options.get('max-upload-limit'):
            cmd.append(f'--max-upload-limit={self.options["max-upload-limit"]}')
            
        if self.options.get('referer'):
            cmd.append(f'--referer={self.options["referer"]}')
        if self.options.get('user-agent'):
            cmd.append(f'--user-agent={self.options["user-agent"]}')
        if self.options.get('header'):
            for header in self.options['header'].splitlines():
                header = header.strip()
                if header:
                    cmd.append(f'--header={header}')
                    
        cmd.append('--allow-overwrite=true')
        cmd.append('--auto-file-renaming=false')
        
        if self.out_name:
            cmd += ['-d', self.out_dir, '-o', self.out_name]
        else:
            cmd += ['-d', self.out_dir]
            
        extra_args = self.options.get('extra_args', '')
        if extra_args:
            try:
                cmd.extend(shlex.split(extra_args))
            except:
                cmd.extend(extra_args.split())
                
        cmd.append(self.url)
        
        return cmd

    def start(self, aria2_path):
        if self.process:
            self.log("Task is already in progress.")
            return
            
        cmd = self.build_command(aria2_path)
        self.log(f"Start command: {' '.join(cmd)}")
        
        try:
            self.process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, 
                bufsize=1,
                universal_newlines=True,
                encoding='utf-8',
                errors='ignore'
            )
            self.status = "Downloading..."
            self._stop_flag.clear()
            
            self.stdout_thread = threading.Thread(target=self._read_output, daemon=True)
            self.stdout_thread.start()
            
        except Exception as e:
            self.log(f"Failed to start: {str(e)}")
            self.status = "Error"

    def pause(self):
        """Pause download"""
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.log("Download paused")
                self.status = "Paused"
                self._stop_flag.set()
            except Exception as e:
                self.log(f"Failed to pause: {str(e)}")

    def stop(self):
        """Stop download"""
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.log("Download stopped")
                self._stop_flag.set()
            except Exception as e:
                self.log(f"Failed to stop: {str(e)}")
        self.status = "Stopped"

    def resume(self, aria2_path):
        """Resume download"""
        if self.status == "Completed":
            self.log("Task already completed, no need to resume")
            return
            
        if self.process and self.process.poll() is None:
            self.log("Task is still running, cannot resume")
            return
            
        self.log("Resuming download (resume broken download)")
        self.options['continue'] = True
        self.start(aria2_path)

    def _read_output(self):
        """Read process output"""
        if not self.process:
            return
            
        for line in self.process.stdout:
            if self._stop_flag.is_set():
                break
                
            line = line.rstrip('\n')
            self.log(line)
            self._parse_progress(line)
            
        # Wait for process to finish
        if self.process:
            self.process.wait()
            
        # Update status
        if self.status == "Downloading" and not self._stop_flag.is_set():
            return_code = self.process.returncode if self.process else None
            if return_code == 0:
                self.status = "Completed"
                self.log("Download completed")
            else:
                self.status = "Error"
                self.log(f"Download error, return code: {return_code}")

    def _parse_progress(self, line):
        """Parse progress information"""
        match = PROGRESS_RE.search(line)
        if match:
            try:
                self.gid = match.group(1)
                have_str = match.group(2)
                total_str = match.group(3)
                self.progress = int(match.group(4))
                self.connections = int(match.group(5))
                self.dl_speed = match.group(6)
                self.eta = match.group(7).strip()
                self.have_bytes = human_to_bytes(have_str)
                self.total_bytes = human_to_bytes(total_str)
            except Exception as e:
                self.log(f"Failed to parse progress: {str(e)}")
                
        # Detect download completion
        if any(keyword in line.lower() for keyword in 
               ['download complete', 'completed', 'download finished']):
            self.status = "Completed"

    def log(self, message):
        """Log message"""
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        log_entry = f"[{timestamp}] {message}"
        self.log_lines.append(log_entry)

class Aria2DownloaderApp:
    """Main application class"""
    
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1000x700")
        
        self.tasks = []
        self.selected_task_index = None
        
        self._create_widgets()
        self._start_ui_updater()

    def _create_widgets(self):
        """Create UI components"""
        # Top frame
        top_frame = Frame(self.root)
        top_frame.pack(fill=X, padx=10, pady=5)
        
        Label(top_frame, text=APP_TITLE, font=('Arial', 16, 'bold')).pack(anchor=W)
        
        # aria2c path setting
        path_frame = Frame(top_frame)
        path_frame.pack(fill=X, pady=5)
        
        Label(path_frame, text="aria2c Path:").pack(side=LEFT)
        self.aria2_path_var = StringVar(value=DEFAULT_ARIA2)
        Entry(path_frame, textvariable=self.aria2_path_var, width=50).pack(side=LEFT, padx=5)
        Button(path_frame, text="Browse", command=self._select_aria2_path).pack(side=LEFT)

        # Download input area
        input_frame = Frame(self.root)
        input_frame.pack(fill=X, padx=10, pady=5)
        
        Label(input_frame, text="Download URL:").grid(row=0, column=0, sticky=W, pady=2)
        self.url_entry = Entry(input_frame, width=80)
        self.url_entry.grid(row=0, column=1, columnspan=4, sticky=W+E, padx=5, pady=2)
        
        Label(input_frame, text="Save Directory:").grid(row=1, column=0, sticky=W, pady=2)
        self.output_dir_var = StringVar(value=os.getcwd())
        Entry(input_frame, textvariable=self.output_dir_var, width=50).grid(row=1, column=1, columnspan=2, sticky=W+E, padx=5, pady=2)
        Button(input_frame, text="Browse", command=self._select_output_dir).grid(row=1, column=3, padx=5, pady=2)
        
        # Fix: Change input_matrix to input_frame
        Label(input_frame, text="Filename:").grid(row=1, column=4, sticky=W, pady=2)
        self.filename_entry = Entry(input_frame, width=20)
        self.filename_entry.grid(row=1, column=5, sticky=W+E, padx=5, pady=2)
        
        Button(input_frame, text="Add Task", command=self._add_task, 
               bg='#4CAF50', fg='white').grid(row=0, column=7, rowspan=2, padx=25, pady=2)

        # Options frame
        options_frame = LabelFrame(self.root, text="Download Options")
        options_frame.pack(fill=X, padx=10, pady=5)
        
        # First row options
        row1 = Frame(options_frame)
        row1.pack(fill=X, pady=2)
        
        self.resume_var = BooleanVar(value=True)
        Checkbutton(row1, text="Resume Download", variable=self.resume_var).pack(side=LEFT, padx=5)
        
        Label(row1, text="Splits:").pack(side=LEFT, padx=(20,0))
        self.split_var = IntVar(value=4)
        Entry(row1, textvariable=self.split_var, width=5).pack(side=LEFT, padx=2)
        
        Label(row1, text="Max Connections:").pack(side=LEFT, padx=(20,0))
        self.connections_var = IntVar(value=16)
        Entry(row1, textvariable=self.connections_var, width=5).pack(side=LEFT, padx=2)
        
        Label(row1, text="Retries:").pack(side=LEFT, padx=(20,0))
        self.retries_var = IntVar(value=5)
        Entry(row1, textvariable=self.retries_var, width=5).pack(side=LEFT, padx=2)

        # Second row options
        row2 = Frame(options_frame)
        row2.pack(fill=X, pady=2)
        
        Label(row2, text="Download Limit:").pack(side=LEFT)
        self.dl_limit_var = StringVar()
        Entry(row2, textvariable=self.dl_limit_var, width=10).pack(side=LEFT, padx=2)
        
        Label(row2, text="User-Agent:").pack(side=LEFT, padx=(20,0))
        self.user_agent_var = StringVar()
        Entry(row2, textvariable=self.user_agent_var, width=30).pack(side=LEFT, padx=2)
        
        Label(row2, text="Referer:").pack(side=LEFT, padx=(20,0))
        self.referer_var = StringVar()
        Entry(row2, textvariable=self.referer_var, width=20).pack(side=LEFT, padx=2)

        # Main content area
        content_frame = Frame(self.root)
        content_frame.pack(fill=BOTH, expand=True, padx=10, pady=5)
        
        # Task list
        list_frame = Frame(content_frame)
        list_frame.pack(side=LEFT, fill=BOTH, expand=True)
        
        Label(list_frame, text="Task List", font=('Arial', 12, 'bold')).pack(anchor=W)
        
        columns = ('url', 'status', 'progress')
        self.task_tree = ttk.Treeview(list_frame, columns=columns, show='headings', height=15)
        self.task_tree.heading('url', text='Download URL')
        self.task_tree.heading('status', text='Status')
        self.task_tree.heading('progress', text='Progress')
        self.task_tree.column('url', width=400)
        self.task_tree.column('status', width=100)
        self.task_tree.column('progress', width=80)
        self.task_tree.pack(fill=BOTH, expand=True, pady=5)
        self.task_tree.bind('<<TreeviewSelect>>', self._on_task_select)
        
        # Task operation buttons
        button_frame = Frame(list_frame)
        button_frame.pack(fill=X, pady=5)
        
        Button(button_frame, text="Start", command=self._start_task, width=8).pack(side=LEFT, padx=2)
        Button(button_frame, text="Pause", command=self._pause_task, width=8).pack(side=LEFT, padx=2)
        Button(button_frame, text="Resume", command=self._resume_task, width=8).pack(side=LEFT, padx=2)
        Button(button_frame, text="Stop", command=self._stop_task, width=8).pack(side=LEFT, padx=2)
        Button(button_frame, text="Delete", command=self._delete_task, width=8).pack(side=LEFT, padx=2)

        # Task details and logs
        detail_frame = Frame(content_frame)
        detail_frame.pack(side=RIGHT, fill=BOTH, expand=True, padx=(10,0))
        
        # Task details
        info_frame = LabelFrame(detail_frame, text="Task Details")
        info_frame.pack(fill=X, pady=(0,5))
        
        self.detail_text = Text(info_frame, height=8, wrap=WORD)
        scrollbar = Scrollbar(info_frame, command=self.detail_text.yview)
        self.detail_text.config(yscrollcommand=scrollbar.set)
        self.detail_text.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)
        
        # Progress bar
        self.progress_var = DoubleVar()
        self.progress_bar = ttk.Progressbar(detail_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=X, pady=5)
        
        # Log box
        log_frame = LabelFrame(detail_frame, text="Download Log")
        log_frame.pack(fill=BOTH, expand=True)
        
        self.log_text = Text(log_frame, wrap=WORD)
        log_scrollbar = Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.config(yscrollcommand=log_scrollbar.set)
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        log_scrollbar.pack(side=RIGHT, fill=Y)

        # Bottom buttons
        bottom_frame = Frame(self.root)
        bottom_frame.pack(fill=X, padx=10, pady=10)
        
        Button(bottom_frame, text="Clear Log", command=self._clear_log).pack(side=LEFT, padx=5)
        Button(bottom_frame, text="Exit", command=self._quit_app).pack(side=RIGHT, padx=5)

    def _select_aria2_path(self):
        """Select aria2c executable path"""
        path = filedialog.askopenfilename(title="Select aria2c Executable")
        if path:
            self.aria2_path_var.set(path)

    def _select_output_dir(self):
        """Select save directory"""
        directory = filedialog.askdirectory(title="Select Save Directory")
        if directory:
            self.output_dir_var.set(directory)

    def _add_task(self):
        """Add new download task"""
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Warning", "Please enter a download URL")
            return
            
        output_dir = self.output_dir_var.get().strip()
        filename = self.filename_entry.get().strip()
        
        options = {
            'continue': self.resume_var.get(),
            'split': self.split_var.get(),
            'max-connection-per-server': self.connections_var.get(),
            'max-tries': self.retries_var.get(),
            'file-allocation': 'none',
            'max-download-limit': self.dl_limit_var.get().strip(),
            'user-agent': self.user_agent_var.get().strip(),
            'referer': self.referer_var.get().strip(),
            'header': '',
            'extra_args': ''
        }
        
        task = DownloadTask(url, output_dir, filename, options)
        self.tasks.append(task)
        
        # Add to task list
        self.task_tree.insert('', 'end', values=(
            task.url[:80] + '...' if len(task.url) > 80 else task.url,
            task.status,
            f"{task.progress}%"
        ))
        
        self._log(f"Task added: {url}")

    def _on_task_select(self, event):
        """Task selection event handler"""
        selection = self.task_tree.selection()
        if not selection:
            self.selected_task_index = None
            return
            
        # Fix: Properly handle tree view item ID
        item_id = selection[0]
        # Find corresponding task index
        for i, task in enumerate(self.tasks):
            tree_id = f'I{i:03}'
            if item_id == tree_id:
                self.selected_task_index = i
                break
        else:
            self.selected_task_index = None
            
        self._update_task_details()

    def _update_task_details(self):
        """Update task details display"""
        if self.selected_task_index is None or self.selected_task_index >= len(self.tasks):
            return
            
        task = self.tasks[self.selected_task_index]
        
        # Update details text
        self.detail_text.delete(1.0, END)
        details = f"""Download URL: {task.url}
Save Path: {os.path.join(task.out_dir, task.out_name) if task.out_name else task.out_dir}
Status: {task.status}
Progress: {task.progress}%
Downloaded: {task.have_bytes} bytes
Total Size: {task.total_bytes} bytes
Download Speed: {task.dl_speed}
ETA: {task.eta}
Connections: {task.connections}
"""
        self.detail_text.insert(1.0, details)
        
        # Update progress bar
        self.progress_var.set(task.progress)
        
        # Update log
        self.log_text.delete(1.0, END)
        for log_entry in task.log_lines[-100:]:  # Show last 100 log entries
            self.log_text.insert(END, log_entry + '\n')
        self.log_text.see(END)

    def _start_task(self):
        """Start selected task"""
        if self.selected_task_index is None:
            messagebox.showwarning("Warning", "Please select a task first")
            return
            
        task = self.tasks[self.selected_task_index]
        aria2_path = self.aria2_path_var.get()
        
        threading.Thread(target=task.start, args=(aria2_path,), daemon=True).start()
        self._log(f"Starting task: {task.url}")

    def _pause_task(self):
        """Pause selected task"""
        if self.selected_task_index is None:
            messagebox.showwarning("Warning", "Please select a task first")
            return
            
        task = self.tasks[self.selected_task_index]
        task.pause()
        self._log(f"Pausing task: {task.url}")

    def _resume_task(self):
        """Resume selected task"""
        if self.selected_task_index is None:
            messagebox.showwarning("Warning", "Please select a task first")
            return
            
        task = self.tasks[self.selected_task_index]
        aria2_path = self.aria2_path_var.get()
        
        threading.Thread(target=task.resume, args=(aria2_path,), daemon=True).start()
        self._log(f"Resuming task: {task.url}")

    def _stop_task(self):
        """Stop selected task"""
        if self.selected_task_index is None:
            messagebox.showwarning("Warning", "Please select a task first")
            return
            
        task = self.tasks[self.selected_task_index]
        task.stop()
        self._log(f"Stopping task: {task.url}")

    def _delete_task(self):
        """Delete selected task"""
        if self.selected_task_index is None:
            messagebox.showwarning("Warning", "Please select a task first")
            return
            
        task = self.tasks[self.selected_task_index]
        
        if task.status == "Downloading":
            messagebox.showwarning("Warning", "Please stop running tasks first")
            return
            
        # Delete from tree
        for item in self.task_tree.selection():
            self.task_tree.delete(item)
        
        # Delete from task list
        self.tasks.pop(self.selected_task_index)
        self.selected_task_index = None
        
        # Rebuild tree view to keep index synchronized
        self._rebuild_task_tree()
        
        self._log(f"Task deleted: {task.url}")

    def _rebuild_task_tree(self):
        """Rebuild task tree view"""
        # Clear existing tree view
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        
        # Re-add all tasks
        for i, task in enumerate(self.tasks):
            self.task_tree.insert('', 'end', iid=f'I{i:03}', values=(
                task.url[:80] + '...' if len(task.url) > 80 else task.url,
                task.status,
                f"{task.progress}%"
            ))

    def _clear_log(self):
        """Clear log"""
        self.log_text.delete(1.0, END)

    def _log(self, message):
        """Add log message"""
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        self.log_text.insert(END, f"[{timestamp}] {message}\n")
        self.log_text.see(END)

    def _start_ui_updater(self):
        """Start UI update thread"""
        def update_ui():
            while True:
                try:
                    # Update task list display
                    for i, task in enumerate(self.tasks):
                        item_id = f'I{i:03}'  # Generate fixed format ID
                        
                        # Check if item exists, create if not
                        if item_id not in self.task_tree.get_children():
                            self.task_tree.insert('', 'end', iid=item_id, values=(
                                task.url[:80] + '...' if len(task.url) > 80 else task.url,
                                task.status,
                                f"{task.progress}%"
                            ))
                        else:
                            # Update existing item
                            self.task_tree.set(item_id, 'status', task.status)
                            self.task_tree.set(item_id, 'progress', f"{task.progress}%")
                    
                    # Update selected task details
                    if self.selected_task_index is not None and self.selected_task_index < len(self.tasks):
                        self._update_task_details()
                        
                except Exception as e:
                    print(f"UI update error: {e}")
                
                time.sleep(1)
        
        threading.Thread(target=update_ui, daemon=True).start()

    def _quit_app(self):
        """Exit application"""
        # Stop all running tasks
        for task in self.tasks:
            if task.status == "Downloading":
                task.stop()
        
        self.root.quit()
        self.root.destroy()

def main():
    """Main function"""
    root = Tk()
    app = Aria2DownloaderApp(root)
    root.mainloop()

if __name__ == '__main__':
    main()