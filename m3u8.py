import os
import requests
import m3u8
from concurrent.futures import ThreadPoolExecutor, as_completed

# 创建保存TS文件的文件夹
output_folder = "ts_files"
if not os.path.exists(output_folder):
    os.makedirs(output_folder)

# 下载TS文件
def download_ts_file(url, output_file):
    response = requests.get(url, stream=True)
    if response.status_code == 200:
        with open(output_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
    print(f"Downloaded: {output_file}")

# 下载并保存所有TS文件
def download_all_ts_files(m3u8_file):
    # 读取本地m3u8文件内容
    m3u8_obj = m3u8.load(m3u8_file)
    
    # 多线程下载，并显示进度
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for i, segment in enumerate(m3u8_obj.segments):
            ts_url = segment.uri
            output_file = os.path.join(output_folder, f"test{i:011}.ts")
            futures.append(executor.submit(download_ts_file, ts_url, output_file))

        for future in as_completed(futures):
            future.result()  # 等待所有任务完成并处理异常

# 合并所有TS文件为一个MP4文件
def merge_ts_files(output_mp4_file):
    with open(output_mp4_file, 'wb') as f:
        for ts_file in sorted(os.listdir(output_folder)):
            ts_path = os.path.join(output_folder, ts_file)
            with open(ts_path, 'rb') as ts:
                f.write(ts.read())

# 删除所有TS文件
def delete_ts_files():
    for ts_file in os.listdir(output_folder):
        os.remove(os.path.join(output_folder, ts_file))
    os.rmdir(output_folder)

# 主函数
def main(m3u8_file, output_mp4_file):
    download_all_ts_files(m3u8_file)
    merge_ts_files(output_mp4_file)
    delete_ts_files()
    print(f"所有TS文件已合并成: {output_mp4_file}，并已删除所有TS文件")

if __name__ == "__main__":
    m3u8_file = "test.m3u8"  # 替换为你的本地m3u8文件名
    output_mp4_file = "output_videos.mp4"
    main(m3u8_file, output_mp4_file)
