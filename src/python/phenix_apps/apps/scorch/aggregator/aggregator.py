"""
Scorch component for relaying data to an external aggregation server.
"""

import json
import os
import stat
import shlex
from datetime import datetime, timezone
from pathlib import Path

import paramiko
import yaml

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
        actions = self._build_stage_actions(stage, stage_cfg)

        if not actions:
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
            for action in actions:
                self._run_stage_action(ssh, stage, action)
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
            "file_structure": stage_cfg.get("file_structure", []),
            "actions": self._normalize_actions(stage_cfg.get("actions", [])),
        }

    def _normalize_list(self, value) -> list[str]:
        """Coerce a scalar, iterable, or null value into a list of strings."""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)

    def _normalize_actions(self, value) -> list[dict]:
        """Coerce stage actions into a normalized list of dictionaries."""
        if value is None:
            return []
        if isinstance(value, dict):
            value = [value]

        actions = []
        for action in value:
            if not isinstance(action, dict):
                raise ValueError(
                    f"stage actions must be dictionaries, got {type(action).__name__}"
                )
            if "type" not in action:
                raise ValueError("stage actions must include a type")
            actions.append(action)
        return actions

    def _build_stage_actions(self, stage: str, stage_cfg: dict) -> list[dict]:
        """Expand legacy stage keys and typed actions into a single action list."""
        actions = []
        file_structure = self._normalize_list(stage_cfg.get("file_structure", []))
        if stage == "configure" and file_structure:
            actions.append({"type": "mkdirs", "paths": file_structure})

        actions.extend(
            {"type": "cmd", "cmd": cmd}
            for cmd in self._normalize_list(stage_cfg.get("cmds", []))
        )
        actions.extend(
            {"type": "upload", "value": upload}
            for upload in self._normalize_list(stage_cfg.get("upload", []))
        )
        actions.extend(
            {"type": "download", "value": download}
            for download in self._normalize_list(stage_cfg.get("download", []))
        )
        actions.extend(stage_cfg.get("actions", []))
        return actions

    def _run_stage_action(
        self, ssh: paramiko.SSHClient, stage: str, action: dict
    ) -> None:
        """Run a single stage action."""
        action_type = action["type"]

        if action_type == "mkdirs":
            self._create_remote_file_structure(ssh, action.get("paths", []))
            return

        if action_type == "cmd":
            cmd = action.get("cmd") or action.get("command") or action.get("value")
            if not cmd:
                raise ValueError("cmd actions require a command")
            self._log(f"{stage}_remote", "Running remotely", cmd=cmd)
            cmd_out = self._run_remote_command(ssh, cmd)
            self._log(f"{stage}_remote", **cmd_out)
            if cmd_out.get("status") == "error":
                raise RuntimeError(
                    f"remote command failed on {self.ip}: {cmd_out.get('stderr')}"
                )
            return

        if action_type == "upload":
            remote_stage_dir = Path(self.remote_base_dir, stage)
            local_file = action.get("value") or action.get("path")
            if not local_file:
                raise ValueError("upload actions require a source path")
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
            return

        if action_type == "download":
            local_stage_dir = Path(self.base_dir, stage)
            remote_file = action.get("value") or action.get("path")
            if not remote_file:
                raise ValueError("download actions require a source path")
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
            return

        if action_type == "write_stage_timestamp":
            self._write_stage_timestamp(stage, action)
            return

        if action_type == "write_experiment_time":
            self._write_experiment_time(stage, action)
            return

        raise ValueError(f"unsupported aggregator stage action type: {action_type}")

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

    def _create_remote_file_structure(
        self, ssh: paramiko.SSHClient, file_structure: list[str]
    ) -> None:
        """Create the configured directory tree on the remote host."""
        remote_root = self._remote_root()
        directories = [remote_root]

        for relative_path in file_structure:
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

    def _write_stage_timestamp(self, stage: str, action: dict) -> None:
        """Write the current stage timestamp into a YAML file."""
        path_value = action.get("path") or action.get("file")
        if not path_value:
            raise ValueError("write_stage_timestamp actions require a path")

        key = action.get("key", stage)
        if not key:
            raise ValueError("write_stage_timestamp actions require a key")

        timestamp = datetime.now(timezone.utc).isoformat()
        path = Path(path_value)
        if not path.is_absolute():
            path = Path(self.base_dir, path)

        existing: dict = {}
        if path.exists():
            loaded = yaml.safe_load(path.read_text())
            if loaded is None:
                existing = {}
            elif isinstance(loaded, dict):
                existing = loaded
            else:
                raise ValueError(
                    f"timestamp YAML file must contain a mapping: {path}"
                )

        self._set_nested_value(existing, key, timestamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(existing, sort_keys=False))

        self._log(
            f"{stage}_component",
            f"Wrote timestamp to {path}",
            path=str(path),
            key=key,
            timestamp=timestamp,
        )

    def _write_experiment_time(self, stage: str, action: dict) -> None:
        """Write experiment start/stop timestamps using the legacy YAML format."""
        if stage not in {"start", "stop"}:
            raise ValueError("write_experiment_time is only valid for start/stop stages")

        path_value = action.get("path") or action.get("file") or "experiment_time.yaml"
        path = Path(path_value)
        if not path.is_absolute():
            path = Path(self.base_dir, path)

        data = self._load_yaml_mapping(path)
        start_key = "experiment_start"
        stop_key = "experiment_stop"

        if stage == "start":
            if start_key in data:
                raise ValueError(
                    "Cannot write experiment_start because it is already present."
                )
            if stop_key in data:
                raise ValueError(
                    "Cannot write experiment_start because experiment_stop is already present."
                )
            data[start_key] = self._get_timestamp_info()
        else:
            if start_key not in data:
                raise ValueError(
                    "Cannot write experiment_stop because experiment_start is not present."
                )
            if stop_key in data:
                raise ValueError(
                    "Cannot write experiment_stop because it is already present."
                )
            data[stop_key] = self._get_timestamp_info()

        self._write_yaml_mapping(path, data)
        self._log(
            f"{stage}_component",
            f"Wrote experiment time to {path}",
            path=str(path),
            stage=stage,
        )

    def _set_nested_value(self, data: dict, dotted_key: str, value) -> None:
        """Set a dotted key path in a nested dictionary."""
        parts = [part for part in dotted_key.split(".") if part]
        if not parts:
            raise ValueError("timestamp keys must not be empty")

        current = data
        for part in parts[:-1]:
            next_value = current.get(part)
            if next_value is None:
                current[part] = {}
            elif not isinstance(next_value, dict):
                raise ValueError(f"cannot set nested key on non-mapping value: {part}")
            current = current[part]

        current[parts[-1]] = value

    def _get_timestamp_info(self) -> dict[str, str]:
        """Return UTC and local timestamps in the legacy experiment format."""
        utc_now = datetime.now(timezone.utc)
        local_now = datetime.now().astimezone()
        return {
            "zulu": utc_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "local": local_now.isoformat(),
        }

    def _load_yaml_mapping(self, yaml_path: Path) -> dict:
        """Load a YAML mapping from disk, returning an empty mapping when absent."""
        if not yaml_path.exists():
            return {}

        loaded = yaml.safe_load(yaml_path.read_text())
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise ValueError(f"{yaml_path.name} must contain a YAML dictionary.")
        return loaded

    def _write_yaml_mapping(self, yaml_path: Path, data: dict) -> None:
        """Persist a YAML mapping to disk."""
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(yaml.safe_dump(data, sort_keys=False))

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
