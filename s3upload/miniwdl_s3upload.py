"""
Plugin for uploading output files to S3 "progressively," meaning to upload each task's output files
immediately upon task completion, instead of waiting for the whole workflow to finish. (The latter
technique, which doesn't need a plugin at all, is illustrated in ../upload_output_files.sh)

To enable, install this plugin (`pip3 install .` & confirm listed by `miniwdl --version`) and set
the environment variable MINIWDL__S3_PROGRESSIVE_UPLOAD__URI_PREFIX to a S3 URI prefix under which
to store the output files (e.g. "s3://my_bucket/workflow123_outputs"). The prefix should be set
uniquely for each run, to prevent different runs from overwriting each others' outputs.

Shells out to s3parcp, for which the environment must be set up to authorize upload to the
specified bucket (without explicit auth-related arguments).

Deposits into each successful task/workflow run directory and S3 folder, an additional file
outputs.s3.json which copies outputs.json replacing local file paths with the uploaded S3 URIs.
(The JSON printed to miniwdl standard output keeps local paths.)

Limitations:
1) All task output files are uploaded, even ones that aren't top-level workflow outputs. (We can't,
   at the moment of task completion, necessarily predict which files the calling workflow will
   finally output.)
2) Doesn't upload (or rewrite outputs JSON for) workflow output files that weren't generated by a
   task, e.g. outputting an input file, or a file generated by write_lines() etc. in the workflow.
   (We could handle such stragglers by uploading them at workflow completion; it just hasn't been
   needed yet.)
"""

import os
import subprocess
import threading
import json
import logging
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, Optional, Tuple, Union

import WDL
from WDL import Env, Value, values_to_json
from WDL import Type
from WDL.runtime import cache, config
from WDL._util import StructuredLogMessage as _

import boto3
import botocore

s3 = boto3.resource("s3", endpoint_url=os.getenv("AWS_ENDPOINT_URL"))
s3_client = boto3.client("s3", endpoint_url=os.getenv("AWS_ENDPOINT_URL"))


def s3_object(uri: str):
    assert uri.startswith("s3://")
    bucket, key = uri.split("/", 3)[2:]
    return s3.Bucket(bucket).Object(key)


def get_s3_put_prefix(cfg: config.Loader) -> str:
    s3prefix = cfg["s3_progressive_upload"]["uri_prefix"]
    assert s3prefix.startswith("s3://"), "MINIWDL__S3_PROGRESSIVE_UPLOAD__URI_PREFIX invalid"
    return s3prefix


def get_s3_get_prefix(cfg: config.Loader) -> str:
    s3prefix = cfg["s3_progressive_upload"].get("call_cache_get_uri_prefix")
    if not s3prefix:
        return get_s3_put_prefix(cfg)
    assert s3prefix.startswith("s3://"), "MINIWDL__S3_PROGRESSIVE_UPLOAD__CALL_CACHE_GET_URI_PREFIX invalid"
    return s3prefix


def flag_temporary(s3uri):
    uri = urlparse(s3uri)
    bucket, key = uri.hostname, uri.path[1:]
    s3_client.put_object_tagging(
        Bucket=bucket,
        Key=key,
        Tagging={
            'TagSet': [
                {
                    'Key': 'swipe_temporary',
                    'Value': 'true'
                },
            ]
        },
    )


def inode(link: str):
    st = os.stat(os.path.realpath(link))
    return (st.st_dev, st.st_ino)


_uploaded_files: Dict[Tuple[int, int], str] = {}
_cached_files: Dict[Tuple[int, int], Tuple[str, Env.Bindings[Value.Base]]] = {}
_uploaded_files_lock = threading.Lock()


def cache_put(cfg: config.Loader, logger: logging.Logger, key: str, outputs: Env.Bindings[Value.Base]):
    if not (cfg["call_cache"].get_bool("put") and
            cfg["call_cache"]["backend"] == "s3_progressive_upload_call_cache_backend"):
        return

    missing = False

    def cache(v: Union[Value.File, Value.Directory]) -> str:
        nonlocal missing
        missing = missing or inode(str(v.value)) not in _uploaded_files
        if missing:
            return ""
        return _uploaded_files[inode(str(v.value))]

    remapped_outputs = Value.rewrite_env_paths(outputs, cache)
    if not missing:
        uri = os.path.join(get_s3_put_prefix(cfg), "cache", f"{key}.json")
        s3_object(uri).put(Body=json.dumps(values_to_json(remapped_outputs)).encode())
        flag_temporary(uri)
        logger.info(_("call cache insert", cache_file=uri))


class CallCache(cache.CallCache):
    def get(
        self, key: str, inputs: Env.Bindings[Value.Base], output_types: Env.Bindings[Type.Base]
    ) -> Optional[Env.Bindings[Value.Base]]:
        uri = urlparse(get_s3_get_prefix(self._cfg))
        bucket, prefix = uri.hostname, uri.path

        key = os.path.join(prefix, "cache", f"{key}.json")[1:]
        abs_fn = os.path.join(self._cfg["call_cache"]["dir"], f"{key}.json")
        Path(abs_fn).parent.mkdir(parents=True, exist_ok=True)
        try:
            s3_client.download_file(bucket, key, abs_fn)
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] != "404":
                raise e

        return super().get(key, inputs, output_types)

    def put(self, key: str, outputs: Env.Bindings[Value.Base]) -> None:
        if not self._cfg["call_cache"].get_bool("put"):
            return

        def cache(v: Union[Value.File, Value.Directory]) -> str:
            _cached_files[inode(v.value)] = (key, outputs)
            return ""

        with _uploaded_files_lock:
            Value.rewrite_env_paths(outputs, cache)
            cache_put(self._cfg, self._logger, key, outputs)


def task(cfg, logger, run_id, run_dir, task, **recv):
    """
    on completion of any task, upload its output files to S3, and record the S3 URI corresponding
    to each local file (keyed by inode) in _uploaded_files
    """
    logger = logger.getChild("s3_progressive_upload")

    # ignore inputs
    recv = yield recv
    # ignore command/runtime/container
    recv = yield recv

    def upload_file(abs_fn, s3uri):
        s3cp(logger, abs_fn, s3uri)
        # record in _uploaded_files (keyed by inode, so that it can be found from any
        # symlink or hardlink)
        with _uploaded_files_lock:
            _uploaded_files[inode(abs_fn)] = s3uri
            if inode(abs_fn) in _cached_files:
                cache_put(cfg, logger, *_cached_files[inode(abs_fn)])
        logger.info(_("task output uploaded", file=abs_fn, uri=s3uri))

    if not cfg.has_option("s3_progressive_upload", "uri_prefix"):
        logger.debug("skipping because MINIWDL__S3_PROGRESSIVE_UPLOAD__URI_PREFIX is unset")
        yield recv
        return

    if run_id[-1].startswith("download-"):
        yield recv
        return

    s3prefix = get_s3_put_prefix(cfg)

    # for each file under out
    def _raise(ex):
        raise ex

    links_dir = os.path.join(run_dir, "out")
    for output in os.listdir(links_dir):
        abs_output = os.path.join(links_dir, output)
        assert os.path.isdir(abs_output)
        output_contents = [os.path.join(abs_output, fn) for fn in os.listdir(abs_output) if not fn.startswith(".")]
        assert output_contents
        if len(output_contents) == 1 and os.path.isdir(output_contents[0]) and os.path.islink(output_contents[0]):
            # directory output
            _uploaded_files[inode(output_contents[0])] = (
                os.path.join(s3prefix, os.path.basename(output_contents[0])) + "/"
            )
            for (dn, subdirs, files) in os.walk(output_contents[0], onerror=_raise):
                assert dn == output_contents[0] or dn.startswith(output_contents[0] + "/"), dn
                for fn in files:
                    abs_fn = os.path.join(dn, fn)
                    s3uri = os.path.join(s3prefix, os.path.relpath(abs_fn, abs_output))
                    upload_file(abs_fn, s3uri)
        elif len(output_contents) == 1 and os.path.isfile(output_contents[0]):
            # file output
            basename = os.path.basename(output_contents[0])
            abs_fn = os.path.join(abs_output, basename)
            s3uri = os.path.join(s3prefix, basename)
            upload_file(abs_fn, s3uri)
        else:
            # file array output
            assert all(os.path.basename(abs_fn).isdigit() for abs_fn in output_contents), output_contents
            for index_dir in output_contents:
                fns = [fn for fn in os.listdir(index_dir) if not fn.startswith(".")]
                assert len(fns) == 1
                abs_fn = os.path.join(index_dir, fns[0])
                s3uri = os.path.join(s3prefix, fns[0])
                upload_file(abs_fn, s3uri)
    yield recv


def workflow(cfg, logger, run_id, run_dir, workflow, **recv):
    """
    on workflow completion, add a file outputs.s3.json to the run directory, which is outputs.json
    with local filenames rewritten to the uploaded S3 URIs (as previously recorded on completion of
    each task).
    """
    logger = logger.getChild("s3_progressive_upload")

    # ignore inputs
    recv = yield recv

    if cfg.has_option("s3_progressive_upload", "uri_prefix"):
        # write outputs.s3.json using _uploaded_files
        write_outputs_s3_json(
            logger,
            recv["outputs"],
            run_dir,
            os.path.join(get_s3_put_prefix(cfg), *run_id[1:]),
            workflow.name,
        )

    yield recv


def write_outputs_s3_json(logger, outputs, run_dir, s3prefix, namespace):
    # rewrite uploaded files to their S3 URIs
    def rewriter(fd):
        try:
            return _uploaded_files[inode(fd.value)]
        except Exception:
            logger.warning(
                _(
                    "output file or directory wasn't uploaded to S3; keeping local path in outputs.s3.json",
                    path=fd.value,
                )
            )
            return fn

    with _uploaded_files_lock:
        outputs_s3 = WDL.Value.rewrite_env_paths(outputs, rewriter)

    # get json dict of rewritten outputs
    outputs_s3_json = WDL.values_to_json(outputs_s3, namespace=namespace)

    # write to outputs.s3.json
    fn = os.path.join(run_dir, "outputs.s3.json")
    with open(fn, "w") as outfile:
        json.dump(outputs_s3_json, outfile, indent=2)
        outfile.write("\n")
    s3cp(logger, fn, os.environ.get("WDL_OUTPUT_URI", os.path.join(s3prefix, "outputs.s3.json")))


_s3parcp_lock = threading.Lock()


def s3cp(logger, fn, s3uri):
    with _s3parcp_lock:
        cmd = ["s3parcp", fn, s3uri]
        logger.debug(" ".join(cmd))
        rslt = subprocess.run(cmd, stderr=subprocess.PIPE)
        if rslt.returncode != 0:
            logger.error(
                _(
                    "failed uploading output file",
                    cmd=" ".join(cmd),
                    exit_status=rslt.returncode,
                    stderr=rslt.stderr.decode("utf-8"),
                )
            )
            raise WDL.Error.RuntimeError("failed: " + " ".join(cmd))
