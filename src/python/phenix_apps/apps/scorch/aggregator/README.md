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
      cmds:
        - <string>
      upload:
        - <string>
      download:
        - <string>
    start:
      cmds:
        - <string>
      upload:
        - <string>
      download:
        - <string>
    stop:
      cmds:
        - <string>
      upload:
        - <string>
      download:
        - <string>
    cleanup:
      cmds:
        - <string>
      upload:
        - <string>
      download:
        - <string>

  # Optional defaults applied to every stage unless overridden above.
  cmds:
    - <string>
  upload:
    - <string>
  download:
    - <string>
```

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
          file_structure:
            - pcap
            - peat/pre
            - peat/post
            - syslogsync
        start:
          cmds:
            - mkdir -p /var/lib/scorch-aggregation/incoming
          upload:
            - /phenix/images/experiment/files/scorch/run-0/collector:/var/lib/scorch-aggregation/incoming/collector
        stop:
          cmds:
            - /opt/shipper/bin/ship --config /etc/shipper.yml
          download:
            - /var/lib/scorch-aggregation/manifests/latest.json
```
