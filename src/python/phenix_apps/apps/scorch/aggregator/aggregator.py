"""
Scorch component for relaying data to an external aggregation server.
"""

import json
import os
import stat
import shlex
from pathlib import Path

import paramiko

from phenix_apps.apps.scorch import ComponentBase
from phenix_apps.common import utils
from phenix_apps.common.logger import logger


class Aggregator(ComponentBase):
    """
    Stage-driven SSH relay for an external aggregation server.
    """

    def __init__(self):
        """Initialize the aggregator component and immediately run its stage logic."""
        ComponentBase.__init__(self, "aggregator")
        self.aggregator_metadata = []
        self.execute_stage()

    def configure(self):
        """Run the configure stage."""
        self._run("configure")

    def start(self):
        """Run the start stage."""
        self._run("start")

    def stop(self):
        """Run the stop stage."""
        self._run("stop")

    def cleanup(self):
        """Run the cleanup stage."""
        self._run("cleanup")

    def _run(self, stage: str) -> None:
        """Execute one stage by applying commands, uploads, and downloads in order."""
        self._get_args()

        self._log(
            f"{stage}_component", f"{stage.capitalize()} user component: {self.name}"
        )

        stage_cfg = self._get_stage_config(stage)
        cmds = self._normalize_list(stage_cfg.get("cmds", []))
        uploads = self._normalize_list(stage_cfg.get("upload", []))
        downloads = self._normalize_list(stage_cfg.get("download", []))
        self.file_structure = self._normalize_list(stage_cfg.get("file_structure", []))

        if not cmds and not uploads and not downloads:
            self._log(
                f"{stage}_component",
                f"No aggregation actions configured for {stage}",
            )
            self._save_metadata(stage)
            return

        self._log(
            f"{stage}_remote",
            f"{stage.capitalize()} aggregation server {self.ip} with user {self.user}.",
            host=self.ip,
            user=self.user,
        )

        ssh = None
        try:
            ssh = self._connect()
            if stage == "configure":
                self._create_remote_file_structure(ssh)

            for cmd in cmds:
                self._log(f"{stage}_remote", "Running remotely", cmd=cmd)
                cmd_out = self._run_remote_command(ssh, cmd)
                self._log(f"{stage}_remote", **cmd_out)
                if cmd_out.get("status") == "error":
                    raise RuntimeError(
                        f"remote command failed on {self.ip}: {cmd_out.get('stderr')}"
                    )

            if uploads:
                remote_stage_dir = Path(self.remote_base_dir, stage)
                for local_file in uploads:
                    local_path, remote_path = self._split_path_pair(
                        local_file,
                        default_src=Path(self.base_dir),
                        default_dst=remote_stage_dir,
                    )
                    files_out = self._push_remote(ssh, local_path, remote_path)
                    self._log(
                        f"{stage}_remote",
                        f"Uploading {local_path} to {remote_path}",
                        **files_out,
                    )
                    if files_out.get("status") == "error":
                        raise RuntimeError(files_out.get("error", "upload failed"))

            if downloads:
                local_stage_dir = Path(self.base_dir, stage)
                for remote_file in downloads:
                    remote_path, local_path = self._split_path_pair(
                        remote_file,
                        default_src=Path(self.remote_base_dir, stage),
                        default_dst=local_stage_dir,
                    )
                    files_out = self._fetch_remote(ssh, remote_path, local_path)
                    self._log(
                        f"{stage}_remote",
                        f"Downloading {remote_path} to {local_path}",
                        **files_out,
                    )
                    if files_out.get("status") == "error":
                        raise RuntimeError(files_out.get("error", "download failed"))
        except Exception as ex:
            logger.error(ex)
            raise
        finally:
            if ssh is not None:
                ssh.close()

        self._log(
            f"{stage}_component", f"{stage.capitalize()} user component: {self.name}"
        )
        self._save_metadata(stage)

    def _get_args(self) -> None:
        """Load host and stage configuration from the component metadata."""
        if self.metadata is None:
            raise ValueError("No aggregator metadata provided")

        host_md = self.metadata.get("host", self.metadata)

        self.ip = host_md.get("ip")
        if self.ip is None:
            raise ValueError("No host IP provided")

        self.user = host_md.get("user")
        if self.user is None:
            raise ValueError("No host user provided")

        self.password = host_md.get("password")
        self.remote_base_dir = str(
            self.metadata.get("remote_base_dir", "/tmp/phenix-aggregator")
        )
        self.stage_map = self.metadata.get("stages", {})
        self.default_cmds = self._normalize_list(self.metadata.get("cmds", []))
        self.default_uploads = self._normalize_list(self.metadata.get("upload", []))
        self.default_downloads = self._normalize_list(self.metadata.get("download", []))

    def _get_stage_config(self, stage: str) -> dict:
        """Return the stage-specific configuration merged with component defaults."""
        stage_cfg = self.stage_map.get(stage, {})

        return {
            "cmds": stage_cfg.get("cmds", self.default_cmds),
            "upload": stage_cfg.get("upload", self.default_uploads),
            "download": stage_cfg.get("download", self.default_downloads),
        }

    def _normalize_list(self, value) -> list[str]:
        """Coerce a scalar, iterable, or null value into a list of strings."""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    def _split_path_pair(
        self,
        value: str,
        default_src: Path,
        default_dst: Path,
    ) -> tuple[Path, str]:
        """Parse a src[:dst] path pair and resolve relative paths against defaults."""
        src, sep, dst = value.partition(":")
        if not sep:
            src = value
            dst_path = default_dst / Path(value).name
        else:
            dst_path = Path(dst)
            if not dst_path.is_absolute():
                dst_path = default_dst / dst_path

        src_path = Path(src)
        if not src_path.is_absolute():
            src_path = default_src / src_path

        return src_path, str(dst_path)

    def _create_remote_file_structure(self, ssh: paramiko.SSHClient) -> None:
        """Create the configured directory tree on the remote host."""
        remote_root = self._remote_root()
        directories = [remote_root]

        for relative_path in self.file_structure:
            normalized = self._normalize_relative_directory(relative_path)
            directories.append(f"{remote_root}/{shlex.quote(normalized)}")

        command = "mkdir -p " + " ".join(directories)
        self._log(
            "configure_remote",
            "Creating remote file structure",
            cmd=command,
            experiment_name=self.exp_name,
            case_number=self.loop,
            run_number=self.run,
        )
        result = self._run_remote_command(ssh, command)
        self._log("configure_remote", **result)
        if result.get("status") == "error":
            raise RuntimeError(
                f"failed to create remote file structure on {self.ip}: {result.get('stderr') or result.get('error')}"
            )

    def _remote_root(self) -> str:
        """Build the remote base directory for the current experiment and run."""
        return (
            f'"$HOME"/scORCh/{shlex.quote(f"{self.exp_name}_{self.loop}")}'
            f"/{shlex.quote(str(self.run))}"
        )

    def _normalize_relative_directory(self, value: str) -> str:
        """Validate and normalize a file_structure entry into a safe relative path."""
        path = Path(value)
        if path.is_absolute():
            raise ValueError(f"file_structure entries must be relative paths: {value}")
        if ".." in path.parts:
            raise ValueError(
                f"file_structure entries must not escape the base directory: {value}"
            )
        normalized = path.as_posix().strip("/")
        if not normalized:
            raise ValueError("file_structure entries must not be empty")
        return normalized

    def _connect(self) -> paramiko.SSHClient:
        """Open an SSH connection to the configured host."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs = {
            "hostname": self.ip,
            "username": self.user,
        }

        if self.password:
            connect_kwargs["password"] = self.password

        ssh.connect(**connect_kwargs)
        return ssh

    def _run_remote_command(self, ssh: paramiko.SSHClient, command: str) -> dict:
        """Run a remote shell command and return a structured execution result."""
        try:
            _stdin, stdout, stderr = ssh.exec_command(command)
            stdout_text = stdout.read().decode().strip()
            stderr_text = stderr.read().decode().strip()
            exitcode = stdout.channel.recv_exit_status()

            result = {
                "host": self.ip,
                "cmd": command,
                "stdout": stdout_text,
                "stderr": stderr_text,
                "exitcode": exitcode,
                "status": "ok" if exitcode == 0 else "error",
            }
            if exitcode != 0:
                result["error"] = f"remote command exited with code {exitcode}"
            return result
        except Exception as ex:
            logger.error(f"Failed to execute remote command: {command}. Error: {ex}")
            return {
                "host": self.ip,
                "cmd": command,
                "stdout": "",
                "stderr": str(ex),
                "exitcode": 1,
                "status": "error",
                "error": str(ex),
            }

    def _push_remote(
        self, ssh: paramiko.SSHClient, local_path: Path, remote_path: str
    ) -> dict:
        """Upload a file or directory to the remote host."""
        result = {
            "host": self.ip,
            "src": str(local_path),
            "dst": remote_path,
            "status": "ok",
            "error": "",
        }

        sftp = None
        try:
            sftp = ssh.open_sftp()
            if local_path.is_dir():
                self._sftp_put_dir(sftp, local_path, remote_path)
            else:
                self._sftp_mkdirs(sftp, str(Path(remote_path).parent))
                sftp.put(str(local_path), remote_path)
        except Exception as ex:
            result["status"] = "error"
            result["error"] = str(ex)
        finally:
            if sftp is not None:
                sftp.close()

        return result

    def _fetch_remote(
        self, ssh: paramiko.SSHClient, remote_path: str, local_path: Path
    ) -> dict:
        """Download a file or directory from the remote host."""
        result = {
            "host": self.ip,
            "src": remote_path,
            "dst": str(local_path),
            "status": "ok",
            "error": "",
        }

        sftp = None
        try:
            sftp = ssh.open_sftp()
            rstat = sftp.stat(remote_path)
            if stat.S_ISDIR(rstat.st_mode):
                self._sftp_get_dir(sftp, remote_path, local_path)
            else:
                os.makedirs(local_path.parent, exist_ok=True)
                sftp.get(remote_path, str(local_path))
        except Exception as ex:
            result["status"] = "error"
            result["error"] = str(ex)
        finally:
            if sftp is not None:
                sftp.close()

        return result

    def _sftp_get_dir(
        self, sftp: paramiko.SFTPClient, remote_dir: str, local_dir: Path
    ) -> None:
        """Recursively copy a remote directory tree to local storage."""
        os.makedirs(local_dir, exist_ok=True)

        for entry in sftp.listdir_attr(remote_dir):
            remote_path = f"{remote_dir.rstrip('/')}/{entry.filename}"
            local_path = local_dir / entry.filename

            if stat.S_ISDIR(entry.st_mode):
                self._sftp_get_dir(sftp, remote_path, local_path)
            else:
                os.makedirs(local_path.parent, exist_ok=True)
                sftp.get(remote_path, str(local_path))

    def _sftp_put_dir(
        self, sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str
    ) -> None:
        """Recursively copy a local directory tree to the remote host."""
        self._sftp_mkdirs(sftp, remote_dir)

        for entry in local_dir.iterdir():
            remote_path = f"{remote_dir.rstrip('/')}/{entry.name}"
            if entry.is_dir():
                self._sftp_put_dir(sftp, entry, remote_path)
            else:
                self._sftp_mkdirs(sftp, str(Path(remote_path).parent))
                sftp.put(str(entry), remote_path)

    def _sftp_mkdirs(self, sftp: paramiko.SFTPClient, remote_dir: str) -> None:
        """Create remote directories recursively when they do not already exist."""
        remote_dir = remote_dir.strip()
        if not remote_dir or remote_dir == "/":
            return

        try:
            sftp.stat(remote_dir)
            return
        except OSError:
            parent = str(Path(remote_dir).parent)
            if parent and parent != remote_dir:
                self._sftp_mkdirs(sftp, parent)
            sftp.mkdir(remote_dir)

    def _save_metadata(self, stage: str) -> None:
        """Persist the collected metadata for the completed stage."""
        m_path = Path(self.base_dir, f"aggregator_metadata_{stage}.json")
        logger.info(f"Saving aggregator metadata to {m_path}")
        utils.write_json(m_path, self.aggregator_metadata)

    def _log(self, event_type: str, message: str | None = None, **data) -> None:
        """Append a metadata event and emit it to the logger."""
        entry = {
            "event": event_type,
            "message": message,
            "data": data,
        }
        self.aggregator_metadata.append(entry)
        logger.info(json.dumps(entry))


def main():
    Aggregator()


if __name__ == "__main__":
    main()
