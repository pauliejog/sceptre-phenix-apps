# Aggregator Component

Stage-driven Scorch component for relaying data to an external aggregation server over SSH.
On `configure`, it creates the remote base directory:
`$HOME/scORCh/<experiment_name>_<case_number>/<run_number>/`.
The subdirectories under that base are defined by metadata.

```text
type:   aggregator
exe:    phenix-scorch-component-aggregator
stages: configure, start, stop, cleanup
```

## Metadata Options

```yaml
metadata:
  host:
    ip: <string> # (REQUIRED) Remote aggregation server to connect to
    user: <string> # (REQUIRED) SSH user
    password: <string> # (OPTIONAL) SSH password

  remote_base_dir: <string> # (OPTIONAL) Base directory on the remote server for default uploads

  stages:
    configure:
      file_structure:
        - <string> # (OPTIONAL) Relative directory path under the remote base directory
      actions:
        - type: mkdirs # create the remote directory tree under the component base
          paths:
            - <string>
        - type: cmd
          cmd: <string>
        - type: upload
          value: <string> # local[:remote]
        - type: download
          value: <string> # remote[:local]
        - type: write_stage_timestamp
          path: <string> # YAML file to update (relative paths resolve under the component output dir)
          key: <string> # YAML key to update; nested keys use dot notation, like experiment.start
        - type: write_experiment_time
          path: <string> # Writes experiment_start/experiment_stop with zulu/local timestamps
    start:
      actions:
        - type: cmd
          cmd: <string>
        - type: upload
          value: <string>
        - type: download
          value: <string>
        - type: write_stage_timestamp
          path: <string>
          key: <string>
        - type: write_experiment_time
          path: <string>
    stop:
      actions:
        - type: cmd
          cmd: <string>
        - type: upload
          value: <string>
        - type: download
          value: <string>
        - type: write_stage_timestamp
          path: <string>
          key: <string>
        - type: write_experiment_time
          path: <string>
    cleanup:
      actions:
        - type: cmd
          cmd: <string>
        - type: upload
          value: <string>
        - type: download
          value: <string>

  # Optional defaults applied to every stage unless overridden above.
  cmds:
    - <string>
  upload:
    - <string>
  download:
    - <string>
```

Legacy `cmds`, `upload`, and `download` entries still work. `actions` lets you mix
different stage-specific types in one place.

## Action Types

- `mkdirs`: creates remote directories under the experiment base directory during
  `configure`.
- `cmd`: runs a shell command on the aggregation server.
- `upload`: copies a local file or directory to the remote host.
- `download`: copies a remote file or directory back to the local Scorch output.
- `write_stage_timestamp`: writes a timestamp into a YAML mapping at the given key.
  Use dotted keys like `experiment.start` to build nested YAML.
- `write_experiment_time`: writes the script-style `experiment_time.yaml` file with
  `experiment_start` and `experiment_stop`, including `zulu` and `local` timestamps.

## Transfer Syntax

Uploads use `local[:remote]`, where `local` is a path on the Scorch host and `remote`
is the destination on the aggregation server. Downloads use `remote[:local]`.

If no destination is provided:

- uploads default to `${remote_base_dir}/${stage}/<name>`
- downloads default to `${base_dir}/${stage}/<name>`

Relative paths are resolved against the corresponding default directory.

## Directory Bootstrap

The `configure` stage creates the remote base directory and any relative
directories listed in `stages.configure.file_structure`.

If you use `type: write_stage_timestamp`, the component writes or updates the YAML
file with the current stage timestamp. If you want the experiment timing file from
your script, use `type: write_experiment_time` in `start` and `stop`; it writes
`experiment_start` and `experiment_stop` with `zulu` and `local` timestamps and
enforces the same one-time rules.

The component maps:

- `experiment_name` -> `self.exp_name`
- `case_number` -> `self.loop`
- `run_number` -> `self.run`

## Example

```yaml
components:
  - name: aggregator-example
    type: aggregator
    metadata:
      host:
        ip: 10.1.1.1
        user: otis
        password: Password
      remote_base_dir: /var/lib/scorch-aggregation
      stages:
        configure:
          actions:
            - type: mkdirs
              paths:
                - pcap
                - peat/pre
                - peat/post
                - syslogsync
        start:
          actions:
            - type: cmd
              cmd: mkdir -p /var/lib/scorch-aggregation/incoming
            - type: upload
              value: /phenix/images/experiment/files/scorch/run-0/collector:/var/lib/scorch-aggregation/incoming/collector
            - type: write_experiment_time
              path: experiment_time.yaml
        stop:
          actions:
            - type: cmd
              cmd: /opt/shipper/bin/ship --config /etc/shipper.yml
            - type: download
              value: /var/lib/scorch-aggregation/manifests/latest.json
            - type: write_experiment_time
              path: experiment_time.yaml
```
