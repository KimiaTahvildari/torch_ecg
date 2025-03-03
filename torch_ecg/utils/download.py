"""
utilities for downloading and extracting compressed data files

For most PhysioNet databases, the WFDB package already has a method `dl_database`
for downloading the data files.

"""

import collections
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import warnings
import zipfile
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Union

import boto3
import requests
from botocore import UNSIGNED
from botocore.client import Config
from tqdm.auto import tqdm

__all__ = [
    "http_get",
]


PHYSIONET_DB_VERSION_PATTERN = "\\d+\\.\\d+\\.\\d+"


def http_get(
    url: str,
    dst_dir: Union[str, bytes, os.PathLike],
    proxies: Optional[dict] = None,
    extract: Literal[True, False, "auto"] = "auto",
    filename: Optional[str] = None,
) -> Path:
    """Download contents of a URL and save to a file.

    This function is a modified version of the `download_file` function in
    `transformers.file_utils` [1]_.

    Parameters
    ----------
    url : str
        URL to download file from.
    dst_dir : `path-like`
        Directory to save the file.
        If `extract` is ``True``, the extracted content will be saved to `dst_dir`.
    proxies : dict, optional
        Dictionary of proxy settings.
    extract : {True, False, "auto"}, default "auto"
        Whether to extract the downloaded file.
        "auto" will extract if the file is a `zip` file or a compressed `tar` file.
    filename : str, optional
        Name of the downloaded file.
        If None, the filename will be the basename of the URL.
        If `extract` is ``True``, the filename will NOT affect the extracted folder name,
        which is set to `dst_dir`, and `filename` is only the downloaded file name.

        .. versionadded:: 0.0.20

    Returns
    -------
    final_dst : pathlib.Path
        Path to the downloaded file or the extracted folder.

    References
    ----------
    .. [1] https://github.com/huggingface/transformers/blob/master/src/transformers/file_utils.py

    """
    Path(dst_dir).mkdir(parents=True, exist_ok=True)
    if filename is not None and (Path(dst_dir) / filename).exists():
        raise FileExistsError("file already exists")
    url_parsed = urllib.parse.urlparse(url)
    if url_parsed.scheme == "":
        # assume https by default
        url = f"https://{url}"
        url_parsed = urllib.parse.urlparse(url)
    if url_parsed.scheme not in ["http", "https", "s3"]:
        raise ValueError(f"Unsupported URL scheme {url_parsed.scheme}")

    assert extract in [True, False, "auto"], "extract must be True, False, or 'auto'."

    if url_parsed.scheme == "s3":
        _download_from_aws_s3_using_awscli(url, dst_dir)
        return Path(dst_dir)

    if url_parsed.netloc == "www.dropbox.com" and url_parsed.query == "dl=0":
        url_parsed = url_parsed._replace(query="dl=1")
        url = url_parsed.geturl()
    # for example "https://www.dropbox.com/s/xxx/test%3F.zip??dl=1"
    # produces pure_url = "https://www.dropbox.com/s/xxx/test?.zip"
    pure_url = urllib.parse.unquote(url_parsed._replace(query="").geturl())

    if url_parsed.netloc == "drive.google.com":
        assert filename is not None, "filename can not be inferred from Google Drive URL."
    if extract == "auto":
        # determine whether to extract based on the file extension
        if filename is not None:
            if is_compressed_file(filename):
                extract = True
            else:
                extract = False
        elif is_compressed_file(url_parsed.path):
            extract = True
        else:
            extract = False

    if url_parsed.netloc == "drive.google.com":
        downloaded_file = tempfile.NamedTemporaryFile(
            dir=dst_dir,
            delete=False,
        )
        _download_from_google_drive(url, downloaded_file.name)
        df_suffix = _suffix(filename)
        downloaded_file.close()
    else:
        print(f"Downloading {url}.")
        if not is_compressed_file(url_parsed.path) and extract:
            if filename is not None:
                if not is_compressed_file(filename):
                    warnings.warn(
                        "filename is given, and it is not a `zip` file or a compressed `tar` file. "
                        "Automatic decompression is turned off.",
                        RuntimeWarning,
                    )
                    extract = False
                else:
                    pass
            else:
                warnings.warn(
                    "URL must be pointing to a `zip` file or a compressed `tar` file. "
                    "Automatic decompression is turned off. "
                    "The user is responsible for decompressing the file manually.",
                    RuntimeWarning,
                )
                extract = False
        parent_dir = Path(dst_dir).parent
        df_suffix = _suffix(pure_url) if filename is None else _suffix(filename)
        downloaded_file = tempfile.NamedTemporaryFile(
            dir=parent_dir,
            suffix=df_suffix,
            delete=False,
        )
        req = requests.get(url, stream=True, proxies=proxies)
        content_length = req.headers.get("Content-Length")
        total = int(content_length) if content_length is not None else None
        if req.status_code in [403, 404]:
            raise Exception(f"Could not reach {url}.")
        progress = tqdm(unit="B", unit_scale=True, total=total, dynamic_ncols=True, mininterval=1.0)
        for chunk in req.iter_content(chunk_size=1024):
            if chunk:  # filter out keep-alive new chunks
                progress.update(len(chunk))
                downloaded_file.write(chunk)
        progress.close()
        downloaded_file.close()

    # add a delay to avoid the error "process cannot access the file because it is being used by another process"
    time.sleep(0.1)

    if extract:
        if ".zip" in df_suffix:
            _unzip_file(str(downloaded_file.name), str(dst_dir))
        # elif ".tar" in df_suffix:  # tar files
        elif re.search("\\.(tar(\\.(gz|bz2|lz|xz|tz|zst))?|tgz|tbz2|tlz|txz|tzst)$", df_suffix) is not None:
            _untar_file(str(downloaded_file.name), str(dst_dir))
        else:
            os.remove(downloaded_file.name)
            raise Exception(f"Unsupported (compressed) archived file type {df_suffix}")
        # avoid the case the compressed file is a folder with the same name
        # DO NOT use _stem(Path(pure_url))
        if filename is None:
            _folder = Path(url).name.replace(_suffix(url), "")
        else:
            _folder = _stem(Path(filename))
        if _folder in os.listdir(dst_dir):
            tmp_folder = str(dst_dir).rstrip(os.sep) + "_tmp"
            # move (rename) the dst_dir to a temporary folder
            os.rename(dst_dir, tmp_folder)
            # move (rename) the extracted folder to the destination folder
            os.rename(Path(tmp_folder) / _folder, dst_dir)
            shutil.rmtree(tmp_folder)
        final_dst = Path(dst_dir)
    else:
        Path(dst_dir).mkdir(parents=True, exist_ok=True)
        if filename is None:
            final_dst = Path(dst_dir) / Path(pure_url).name
        else:
            final_dst = Path(dst_dir) / filename
        shutil.copyfile(downloaded_file.name, final_dst)
    os.remove(downloaded_file.name)
    return final_dst


def _stem(path: Union[str, bytes, os.PathLike]) -> str:
    """Get filename without extension, especially for .tar.xx files.

    Parameters
    ----------
    path : `path-like`
        Path to the file

    Returns
    -------
    str
        Filename without extension.

    """
    ret = Path(path).stem
    if Path(ret).suffix in [".tar", ".gz", ".tz", ".lz", ".bz2", ".xz", ".zip", ".7z"]:
        return _stem(ret)
    return ret


def _suffix(path: Union[str, bytes, os.PathLike], ignore_pattern: str = PHYSIONET_DB_VERSION_PATTERN) -> str:
    """Get file extension, including all suffixes.

    Parameters
    ----------
    path : `path-like`
        Path to the file.
    ignore_pattern : str, optional
        Pattern to ignore in the filename,
        by default `PHYSIONET_DB_VERSION_PATTERN`.

    Returns
    -------
    str
        The full file extension.

    """
    return "".join(Path(re.sub(ignore_pattern, "", str(path))).suffixes)


def is_compressed_file(path: Union[str, bytes, os.PathLike]) -> bool:
    """Check if the path points to a valid compressed file.

    NOTE: a `.pth.tar` file is NOT considered a compressed file.

    Parameters
    ----------
    path : `path-like`
        Path to the file

    Returns
    -------
    bool
        True if the file is a valid compressed file,
        False otherwise.

    """
    # compressed_file_pattern = "(\\.zip)|(\\.tar)"
    compressed_file_pattern = "^(?!.*\\.pth\\.tar$).*\\.(zip|7z|tar(\\.(gz|bz2|lz|xz|tz|zst))?|tgz|tbz2|tlz|txz|tzst)$"
    return re.search(compressed_file_pattern, _suffix(path)) is not None


def _unzip_file(path_to_zip_file: Union[str, bytes, os.PathLike], dst_dir: Union[str, bytes, os.PathLike]) -> None:
    """Unzips a .zip file to folder path.

    Parameters
    ----------
    path_to_zip_file : `path-like`
        Path to the .zip file.
    dst_dir : `path-like`
        Path to the destination folder.

    Returns
    -------
    None

    """
    print(f"Extracting file {path_to_zip_file} to {dst_dir}.")
    with zipfile.ZipFile(str(path_to_zip_file)) as zip_ref:
        zip_ref.extractall(str(dst_dir))


def _untar_file(path_to_tar_file: Union[str, bytes, os.PathLike], dst_dir: Union[str, bytes, os.PathLike]) -> None:
    """Decompress a .tar.xx file to folder path.

    Parameters
    ----------
    path_to_tar_file : `path-like`
        Path to the .tar.xx file.
    dst_dir : `path-like`
        Path to the destination folder.

    Returns
    -------
    None

    """
    print(f"Extracting file {path_to_tar_file} to {dst_dir}.")
    mode = Path(path_to_tar_file).suffix.replace(".", "r:").replace("tar", "").strip(":")
    with tarfile.open(str(path_to_tar_file), mode) as tar_ref:
        # tar_ref.extractall(str(dst_dir))
        # CVE-2007-4559 (related to  CVE-2001-1267):
        # directory traversal vulnerability in `extract` and `extractall` in `tarfile` module
        _safe_tar_extract(tar_ref, str(dst_dir))


def _is_within_directory(directory: Union[str, bytes, os.PathLike], target: Union[str, bytes, os.PathLike]) -> bool:
    """Check if the target is within the directory.

    Parameters
    ----------
    directory : `path-like`
        Directory to check.
    target : `path-like`
        Path to the target.

    Returns
    -------
    bool
        True if the target is within the directory,
        False otherwise.

    """
    abs_directory = os.path.abspath(directory)
    abs_target = os.path.abspath(target)

    prefix = os.path.commonprefix([abs_directory, abs_target])

    return prefix == abs_directory


def _safe_tar_extract(
    tar: tarfile.TarFile,
    dst_dir: Union[str, bytes, os.PathLike],
    members: Optional[Iterable[tarfile.TarInfo]] = None,
    *,
    numeric_owner: bool = False,
) -> None:
    """Extract members from a tarfile **safely** to a destination directory.

    This function prevents path traversal attacks, by checking that the
    extracted files are within the destination directory.

    Parameters
    ----------
    tar : tarfile.TarFile
        The tarfile to extract from.
    dst_dir : `path-like`
        The destination directory
    members : Iterable[tarfile.TarInfo], optional
        The members to extract
        If is None, extract all members,
        otherwise, must be a subset of the list
        returned by :func:`tar.getmembers`.
    numeric_owner : bool, default False
        If True, only the numbers for user/group names are used
        and not the names. For more information,
        see :func:`tarfile.TarFile.extractall`.

    Returns
    -------
    None

    """
    for member in members or tar.getmembers():
        member_path = os.path.join(dst_dir, member.name)
        if not _is_within_directory(dst_dir, member_path):
            raise Exception("Attempted Path Traversal in Tar File")

    tar.extractall(dst_dir, members, numeric_owner=numeric_owner)


def url_is_reachable(url: str, **kwargs: Any) -> bool:
    """Check if a URL is reachable.

    Parameters
    ----------
    url : str
        The URL.
    **kwargs : dict, optional
        Additional keyword arguments to pass to :meth:`requests.head`.

    Returns
    -------
    bool
        Whether the URL is reachable.

    """
    try:
        timeout = kwargs.pop("timeout", 3)
        r = requests.head(url, timeout=timeout, **kwargs)
        # successful responses and redirection messages
        # https://developer.mozilla.org/en-US/docs/Web/HTTP/Status#information_responses
        # Informational responses (100 – 199)
        # Successful responses (200 – 299)
        # Redirection messages (300 – 399)
        # Client error responses (400 – 499)
        # Server error responses (500 – 599)
        return 100 <= r.status_code < 400
    except Exception:
        return False


def _download_from_google_drive(url_or_id: str, output: Union[str, bytes, os.PathLike], quiet: bool = False) -> None:
    """Download a file from Google Drive.

    Parameters
    ----------
    url_or_id : str
        The URL of the file or the file ID.
    output : `path-like`
        The output file path.
    quiet : bool, default False
        Whether to suppress the output.

    Returns
    -------
    None

    """
    try:
        import gdown
    except (ImportError, ModuleNotFoundError):
        raise ImportError("gdown is required to download from Google Drive.")
    file_id_pattern = "^1[A-Za-z0-9-_]{32}$"
    if re.match(file_id_pattern, url_or_id) is not None:
        url_or_id = f"https://drive.google.com/u/0/uc?id={url_or_id}"
    url_parsed = urllib.parse.urlparse(url_or_id)
    if url_parsed.scheme == "" and url_parsed.netloc == "":
        # no scheme is given, e.g. drive.google.com/file/d/xxx/view?usp=sharing
        url_or_id = f"https://{url_or_id}"
        url_parsed = urllib.parse.urlparse(url_or_id)
    # remove trailing query string
    url_or_id = re.sub("/view\\?.*$", "", url_or_id)
    if re.match("^https://drive\\.google.com/file/d/", url_or_id) is not None:
        url_or_id = re.sub(
            "^https://drive\\.google\\.com/file/d/",
            "https://drive.google.com/u/0/uc?id=",
            url_or_id,
        )
        print(f"Redirecting to {url_or_id}")
    gdown.download(url_or_id, str(output), quiet=quiet)


def count_aws_s3_bucket(bucket_name: str, prefix: str = "") -> int:
    """Count the number of objects in an AWS S3 bucket.

    Parameters
    ----------
    bucket_name : str
        The bucket name.
    prefix : str, optional
        The prefix to filter the objects,
        for example, "ludb/1.0.1/".

    Returns
    -------
    int
        The number of objects in the bucket.

    """
    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
    paginator = s3.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

    object_count = 0
    for page in page_iterator:
        if "Contents" in page:
            object_count += len(page["Contents"])

    s3.close()

    return object_count


def _download_from_aws_s3_using_boto3(url: str, dst_dir: Union[str, bytes, os.PathLike]) -> None:
    """Download a file from AWS S3 using boto3.

    Currently, much slower than using awscli.

    Parameters
    ----------
    url : str
        The URL of the file.
        For example, "s3://bucket-name/files/pre-fix".
    dst_dir : `path-like`
        The output directory.

    Returns
    -------
    None

    """
    pattern = "^s3://(?P<bucket_name>[^/]+)/(?P<prefix>.+)$"
    match = re.match(pattern, url)
    if match is None:
        raise ValueError(f"Invalid S3 URL: {url}")
    bucket_name = match.group("bucket_name")
    prefix = match.group("prefix")
    if prefix.startswith("files/"):
        prefix = prefix.replace("files/", "")

    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))
    paginator = s3.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)
    object_count = 0
    for page in page_iterator:
        if "Contents" in page:
            object_count += len(page["Contents"])
    if object_count == 0:
        raise ValueError(f"No objects found in S3 bucket: {bucket_name}")
    print(f"Downloading from S3 bucket: {bucket_name}, prefix: {prefix}, total files: {object_count}")

    with tqdm(total=object_count, dynamic_ncols=True, mininterval=1.0) as pbar:
        for page in page_iterator:
            if "Contents" in page:
                for obj in page["Contents"]:
                    key = obj["Key"]
                    dst_file = Path(dst_dir) / key
                    if not dst_file.parent.exists():
                        dst_file.parent.mkdir(parents=True, exist_ok=True)
                        s3.download_file(bucket_name, key, str(dst_file))
                    pbar.update(1)

    s3.close()


def _download_from_aws_s3_using_awscli(url: str, dst_dir: Union[str, bytes, os.PathLike]) -> None:
    """Download a file from AWS S3 using awscli.

    Parameters
    ----------
    url : str
        The URL of the file.
        For example, "s3://bucket-name/files/pre-fix".
    dst_dir : `path-like`
        The output directory.

    Returns
    -------
    None

    """
    assert shutil.which("aws") is not None, "AWS cli is required to download from S3."

    pattern = "^s3://(?P<bucket_name>[^/]+)/(?P<prefix>.+)$"
    match = re.match(pattern, url)
    if match is None:
        raise ValueError(f"Invalid S3 URL: {url}")
    bucket_name = match.group("bucket_name")
    prefix = match.group("prefix")
    if prefix.startswith("files/"):
        prefix = prefix.replace("files/", "")
    object_count = count_aws_s3_bucket(bucket_name, prefix)
    print(f"Downloading from S3 bucket: {bucket_name}, prefix: {prefix}, total files: {object_count}")

    pbar = tqdm(total=object_count, dynamic_ncols=True, mininterval=1.0)
    download_count = 0
    command = f"aws s3 sync --no-sign-request {url} {dst_dir}"
    process = subprocess.Popen(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    debug_stdout = collections.deque(maxlen=10)
    while 1:
        line = process.stdout.readline().decode("utf-8", errors="replace")
        if line.rstrip():
            debug_stdout.append(line)
            if "download: s3:" in line:
                download_count += 1
                pbar.update(1)
        exitcode = process.poll()
        if exitcode is not None:
            for line in process.stdout:
                debug_stdout.append(line.decode("utf-8", errors="replace"))
            if exitcode is not None and exitcode != 0:
                error_msg = "\n".join(debug_stdout)
                process.communicate()
                process.stdout.close()
                raise subprocess.CalledProcessError(exitcode, error_msg)
            else:
                break
    process.communicate()
    process.stdout.close()
    # object_count - download_count files skipped for they already exist
    pbar.update(object_count - download_count)
    pbar.close()
    print(f"Downloaded {download_count} files from S3.")
