#include <opencv2/opencv.hpp>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <vector>

int main() {
    const int W = 384, H = 216;
    FILE *pipe = popen(
        "ffmpeg -v error -hwaccel cuda -hwaccel_output_format cuda "
        "-i /var/www/dev_aduana/alt_batch2/videos/cam1_20260901_144704.mkv "
        "-vf scale_cuda=384:216,hwdownload,format=nv12 -f rawvideo -pix_fmt bgr24 pipe:1",
        "r");
    std::vector<uchar> buf((size_t)W * H * 3);
    cv::Mat frame(H, W, CV_8UC3);
    long n = 0;
    auto t0 = std::chrono::steady_clock::now();
    while (true) {
        size_t got = fread(buf.data(), 1, buf.size(), pipe);
        if (got < buf.size())
            break;
        std::memcpy(frame.data, buf.data(), buf.size());
        cv::imshow("Camera", frame);
        if (cv::waitKey(1) == 'q')
            break;
        n++;
    }
    auto t1 = std::chrono::steady_clock::now();
    pclose(pipe);
    cv::destroyAllWindows();
    double secs = std::chrono::duration<double>(t1 - t0).count();
    std::cout << "CPP frames=" << n << " elapsed=" << secs << "s ("
              << n / secs << " fps)" << std::endl;
    return 0;
}
