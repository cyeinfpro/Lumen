# Image 2.6 公开案例摘要（2026-11-05）

本文件从保留在本地的73份原始结果生成白名单摘要，不是新的模型请求。保留参数、结果、错误及媒体SHA；删除请求/响应关联ID、安全标识、绝对路径和无关元数据。完整范围见[精选证据说明](image26-evidence-publication.md)。媒体文件名仅用于档案追溯，不表示完整媒体已上传。

## g01-flare-gif-default

```json
{
  "case": "g01-flare-gif-default",
  "source_result_sha256": "041e73ed6450e8ea34877466095550c97f1c51e0630daec9da00fff822d93da7",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Create a seamless looping animated GIF of a small orange robot waving its right hand. White background, fixed camera, simple flat shapes.",
    "size": "auto",
    "quality": "low",
    "output_format": "gif"
  },
  "http_status": 200,
  "elapsed_s": 14.861,
  "decoded_assets": [
    {
      "bytes": 397698,
      "sha256": "80afd07b348fa1dd4d812df141b656667c311a76e58dfa04079eb26cf2a65eb0",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 9,
      "loop": 0,
      "durations_ms": [
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200
      ],
      "distinct_frames": 9,
      "duration_total_ms": 1800
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 397698,
      "sha256": "80afd07b348fa1dd4d812df141b656667c311a76e58dfa04079eb26cf2a65eb0"
    }
  ]
}
```

## g02-sunburst-gif

```json
{
  "case": "g02-sunburst-gif",
  "source_result_sha256": "31b115adcae6572e0a5715839d5b179c6bae8cb5fc4277c33153ff96731736cf",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-sunburst",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 18.181,
  "decoded_assets": [
    {
      "bytes": 167125,
      "sha256": "5f4b50720c51b98637ddab76680a4b7cbe861eb5921b2b84ac59b74d69c305cb",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 167125,
      "sha256": "5f4b50720c51b98637ddab76680a4b7cbe861eb5921b2b84ac59b74d69c305cb"
    }
  ]
}
```

## g03-edit-multipart-gif

```json
{
  "case": "g03-edit-multipart-gif",
  "source_result_sha256": "ec71c2ed4c21b0988d55f855d4db0155635abf09c65cc9ff860eb8877e44fe51",
  "endpoint": "/images/edits",
  "encoding": "multipart",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate the supplied orange robot waving its raised right hand. Preserve its face, orange color and white background. Seamless loop, fixed camera.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 8.397,
  "decoded_assets": [
    {
      "bytes": 87711,
      "sha256": "ff2131e23f721e7b264bb0b643d9161bef099a376b169c4c55bad17ba14dff9d",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 87711,
      "sha256": "ff2131e23f721e7b264bb0b643d9161bef099a376b169c4c55bad17ba14dff9d"
    }
  ]
}
```

## g04-edit-json-gif

```json
{
  "case": "g04-edit-json-gif",
  "source_result_sha256": "85e8c2f8bcde163bd52c1c6d3ec83421e7925780496314a8e0be8e28d05460c5",
  "endpoint": "/images/edits",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate this orange robot blinking its two blue eyes. Preserve the appearance and composition. Seamless looping GIF.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 10.127,
  "decoded_assets": [
    {
      "bytes": 79764,
      "sha256": "8158b35c0852324fa702af6b2d41e577c10d91effe550b55280e7389c476ef85",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 79764,
      "sha256": "8158b35c0852324fa702af6b2d41e577c10d91effe550b55280e7389c476ef85"
    }
  ]
}
```

## g10-size-256x256

```json
{
  "case": "g10-size-256x256",
  "source_result_sha256": "564e9977dc959409f8d65fb9a9b42d935c0c3ab4c187f52659dda60a9ce7f4ff",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless animation of an orange ball bouncing on a white floor. Fixed camera.",
    "size": "256x256",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 400,
  "elapsed_s": 0.4,
  "error": {
    "code": "invalid_value",
    "message": "No supported sheet fits 4 GIF frames at size '256x256'. Invalid size '1024x512'. Requested resolution is below the current minimum pixel budget. Invalid size '512x1024'. Requested resolution is below the current minimum pixel budget.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g11-size-512x256

```json
{
  "case": "g11-size-512x256",
  "source_result_sha256": "f2433290d0bb9e444df7f322e933dce410a4693c65a7ab80bc1819488661c864",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless animation of an orange ball bouncing on a white floor. Fixed camera.",
    "size": "512x256",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 7.393,
  "decoded_assets": [
    {
      "bytes": 58514,
      "sha256": "67fa443eec9445e9304f35b3f4d98f04b2c09ccf2291d79556c939e4a40c2075",
      "format": "GIF",
      "width": 512,
      "height": 256,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 58514,
      "sha256": "67fa443eec9445e9304f35b3f4d98f04b2c09ccf2291d79556c939e4a40c2075"
    }
  ]
}
```

## g12-size-256x512

```json
{
  "case": "g12-size-256x512",
  "source_result_sha256": "c7ea84f91df62db541187aaf626430975976c67505b94d5c4d1fc2d9d68c3842",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless animation of an orange ball bouncing on a white floor. Fixed camera.",
    "size": "256x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 7.176,
  "decoded_assets": [
    {
      "bytes": 52826,
      "sha256": "bd08349bb8eaa343fb48566015b5ed011cee6b3631dab7e1dc00a0a1c86bcfef",
      "format": "GIF",
      "width": 256,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 52826,
      "sha256": "bd08349bb8eaa343fb48566015b5ed011cee6b3631dab7e1dc00a0a1c86bcfef"
    }
  ]
}
```

## g13-size-768x512

```json
{
  "case": "g13-size-768x512",
  "source_result_sha256": "c33206afe1e921682ea2a72a2f2d567c031a90c9c379ba466d85b6c377dac0ca",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless animation of an orange ball bouncing on a white floor. Fixed camera.",
    "size": "768x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 9.845,
  "decoded_assets": [
    {
      "bytes": 180769,
      "sha256": "72ee08b8bf429df5f51115f367e77c649a47b3d20311be8eecb48e8f593f2006",
      "format": "GIF",
      "width": 768,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 180769,
      "sha256": "72ee08b8bf429df5f51115f367e77c649a47b3d20311be8eecb48e8f593f2006"
    }
  ]
}
```

## g14-size-1024x1024

```json
{
  "case": "g14-size-1024x1024",
  "source_result_sha256": "c27983a49911ceb22a1c6561308d15124f5b75e943a40af9c1e62f6a56100514",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless animation of an orange ball bouncing on a white floor. Fixed camera.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 400,
  "elapsed_s": 0.59,
  "error": {
    "code": "invalid_value",
    "message": "No supported sheet fits 4 GIF frames at size '1024x1024'. Invalid size '4096x2048'. The longest edge must be less than or equal to 3840. Invalid size '2048x4096'. The longest edge must be less than or equal to 3840.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g20-frames-2

```json
{
  "case": "g20-frames-2",
  "source_result_sha256": "67de59ce96eac52a34d44d10ea7792a6345fdac1cb06cb44092eaf51f693f5bc",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless looping animation of a blue circle moving left and right on white background.",
    "size": "256x256",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 2,
    "frame_duration_ms": 20
  },
  "http_status": 400,
  "elapsed_s": 0.624,
  "error": {
    "code": "invalid_value",
    "message": "No supported sheet fits 2 GIF frames at size '256x256'. Invalid size '1024x512'. Requested resolution is below the current minimum pixel budget. Invalid size '512x1024'. Requested resolution is below the current minimum pixel budget.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g21-frames-16

```json
{
  "case": "g21-frames-16",
  "source_result_sha256": "c652c9ecacebcf5024e8cc4e2c165e176167a9cd1c371ea2d20bff71a32afc41",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless looping animation of a blue circle moving left and right on white background.",
    "size": "256x256",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 16,
    "frame_duration_ms": 33
  },
  "http_status": 200,
  "elapsed_s": 9.481,
  "decoded_assets": [
    {
      "bytes": 86804,
      "sha256": "498e41aa23666ebd065c813765e762d8d0a043d8b429f42e1b6855f0836b0e4d",
      "format": "GIF",
      "width": 256,
      "height": 256,
      "frames": 16,
      "loop": 0,
      "durations_ms": [
        30,
        30,
        30,
        30,
        30,
        30,
        30,
        30,
        30,
        30,
        30,
        30,
        30,
        30,
        30,
        30
      ],
      "distinct_frames": 16,
      "duration_total_ms": 480
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 86804,
      "sha256": "498e41aa23666ebd065c813765e762d8d0a043d8b429f42e1b6855f0836b0e4d"
    }
  ]
}
```

## g22-frames-17

```json
{
  "case": "g22-frames-17",
  "source_result_sha256": "f0b28369e86219c9d33e53390be872e0a5fe236b6bf2f917ed51dcb3e97cfca4",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless looping animation of a blue circle moving left and right on white background.",
    "size": "256x256",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 17,
    "frame_duration_ms": 200
  },
  "http_status": 400,
  "elapsed_s": 0.398,
  "error": {
    "code": "invalid_value",
    "message": "Invalid 'frame_count' for GIF output. Received 17. Use an integer from 2 to 16, or omit 'frame_count' to use 9.",
    "param": "frame_count",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g23-frames-64

```json
{
  "case": "g23-frames-64",
  "source_result_sha256": "8117f20c329f3cfa5ae43d9294f559123dfddcd0b4cdabb8a7fbda59a0388fbc",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless looping animation of a blue circle moving left and right on white background.",
    "size": "256x256",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 64,
    "frame_duration_ms": 200
  },
  "http_status": 400,
  "elapsed_s": 0.596,
  "error": {
    "code": "invalid_value",
    "message": "Invalid 'frame_count' for GIF output. Received 64. Use an integer from 2 to 16, or omit 'frame_count' to use 9.",
    "param": "frame_count",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g24-frames-65

```json
{
  "case": "g24-frames-65",
  "source_result_sha256": "825579467446a99ec0ae7d35bb102c734f0c80f5191c06fde1c8672dd47c5814",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Seamless looping animation of a blue circle moving left and right on white background.",
    "size": "256x256",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 65,
    "frame_duration_ms": 200
  },
  "http_status": 400,
  "elapsed_s": 0.367,
  "error": {
    "code": "invalid_value",
    "message": "Invalid 'frame_count' for GIF output. Received 65. Use an integer from 2 to 16, or omit 'frame_count' to use 9.",
    "param": "frame_count",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g25-count2-valid

```json
{
  "case": "g25-count2-valid",
  "source_result_sha256": "a6a2770aeef8f8cea24fcf627b51a92896053b974f46b5fa85ba339be933bfec",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 2,
    "frame_duration_ms": 20
  },
  "http_status": 200,
  "elapsed_s": 8.822,
  "decoded_assets": [
    {
      "bytes": 71807,
      "sha256": "6c206b58fc8020de8a4a7d62538ebe035577c0d65d76f98078d3e5ea4a5256a9",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 2,
      "loop": 0,
      "durations_ms": [
        20,
        20
      ],
      "distinct_frames": 2,
      "duration_total_ms": 40
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 71807,
      "sha256": "6c206b58fc8020de8a4a7d62538ebe035577c0d65d76f98078d3e5ea4a5256a9"
    }
  ]
}
```

## g30-delay-15

```json
{
  "case": "g30-delay-15",
  "source_result_sha256": "0d5fbb0c170a5208e51c7e243faf13e7252c20134ad7904ff73fff84429a309c",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate a blue ball rolling from left to right and back, white background.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 15
  },
  "http_status": 400,
  "elapsed_s": 0.39,
  "error": {
    "code": "integer_below_min_value",
    "message": "Invalid 'frame_duration_ms': integer below minimum value. Expected a value >= 20, but got 15 instead.",
    "param": "frame_duration_ms",
    "type": "invalid_request_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g31-delay-40

```json
{
  "case": "g31-delay-40",
  "source_result_sha256": "3520a9ddc5ff9d837aa20589cc6e6ff9bbb723f3ba6266b33e128d773906c723",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate a blue ball rolling from left to right and back, white background.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 40
  },
  "http_status": 200,
  "elapsed_s": 8.921,
  "decoded_assets": [
    {
      "bytes": 72186,
      "sha256": "c4b08a17508a8629b197df0d045725595cfe47b2e6cd153cbfd1e9cbfa7d8f71",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        40,
        40,
        40,
        40
      ],
      "distinct_frames": 4,
      "duration_total_ms": 160
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 72186,
      "sha256": "c4b08a17508a8629b197df0d045725595cfe47b2e6cd153cbfd1e9cbfa7d8f71"
    }
  ]
}
```

## g32-delay-66

```json
{
  "case": "g32-delay-66",
  "source_result_sha256": "45489c1fec841c2961c1efe50239fd4f4ec398a0a2743c98f5c99e295567e121",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate a blue ball rolling from left to right and back, white background.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 66
  },
  "http_status": 200,
  "elapsed_s": 14.157,
  "decoded_assets": [
    {
      "bytes": 80913,
      "sha256": "25d4fb13b88ddaaec82a9286848311c9538cfdffa45989ce286fe1637091fdfe",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        70,
        70,
        70,
        70
      ],
      "distinct_frames": 4,
      "duration_total_ms": 280
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 80913,
      "sha256": "25d4fb13b88ddaaec82a9286848311c9538cfdffa45989ce286fe1637091fdfe"
    }
  ]
}
```

## g33-delay-125

```json
{
  "case": "g33-delay-125",
  "source_result_sha256": "2606a493b78d62ac94ed43d8d859164a42fcf82f0392c288215a388e34ab358a",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate a blue ball rolling from left to right and back, white background.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 125
  },
  "http_status": 200,
  "elapsed_s": 7.719,
  "decoded_assets": [
    {
      "bytes": 65743,
      "sha256": "4af258eee2bde353ce24b6d715e987d3f50173a76f0ed606cf33ef5c73cf429a",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        120,
        120,
        120,
        120
      ],
      "distinct_frames": 4,
      "duration_total_ms": 480
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 65743,
      "sha256": "4af258eee2bde353ce24b6d715e987d3f50173a76f0ed606cf33ef5c73cf429a"
    }
  ]
}
```

## g34-delay-2000

```json
{
  "case": "g34-delay-2000",
  "source_result_sha256": "1209a02055e1667b081f34c513240cd8fe3cca78ebdd80cf0090a91c8a08c9ea",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate a blue ball rolling from left to right and back, white background.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 2000
  },
  "http_status": 200,
  "elapsed_s": 9.075,
  "decoded_assets": [
    {
      "bytes": 67649,
      "sha256": "8eebdab48a34ae89bc46b9906bd5004af6c685aa44654f548f3d4587fa36086d",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        2000,
        2000,
        2000,
        2000
      ],
      "distinct_frames": 4,
      "duration_total_ms": 8000
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 67649,
      "sha256": "8eebdab48a34ae89bc46b9906bd5004af6c685aa44654f548f3d4587fa36086d"
    }
  ]
}
```

## g35-delay-2100

```json
{
  "case": "g35-delay-2100",
  "source_result_sha256": "ba62f119359a7d93d3b5139c6bf1b0fea439bef5d8ead2558f7826680a758788",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate a blue ball rolling from left to right and back, white background.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 2100
  },
  "http_status": 200,
  "elapsed_s": 7.457,
  "decoded_assets": [
    {
      "bytes": 59844,
      "sha256": "5069e889c0c031f305b888944fb8653635724de0a61eb1f21e02bd807b11e770",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        2100,
        2100,
        2100,
        2100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 8400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 59844,
      "sha256": "5069e889c0c031f305b888944fb8653635724de0a61eb1f21e02bd807b11e770"
    }
  ]
}
```

## g36-delay-655360

```json
{
  "case": "g36-delay-655360",
  "source_result_sha256": "f0a50c51d48371c429ac42613016ed439a147421489d4123f3d27c8ab452d304",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate a blue ball rolling from left to right and back, white background.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 655360
  },
  "http_status": 200,
  "elapsed_s": 9.143,
  "decoded_assets": [
    {
      "bytes": 73699,
      "sha256": "144fc947f5c8d2df335340cce1c83661f2da88a4f519d3fc181227dbe611f17f",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        655350,
        655350,
        655350,
        655350
      ],
      "distinct_frames": 4,
      "duration_total_ms": 2621400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 73699,
      "sha256": "144fc947f5c8d2df335340cce1c83661f2da88a4f519d3fc181227dbe611f17f"
    }
  ]
}
```

## g40-size-768x768

```json
{
  "case": "g40-size-768x768",
  "source_result_sha256": "f2e44a9bebe647a2526257b83f4690ca41fc0620035cf7e1a891a657f232f6eb",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "768x768",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 13.452,
  "decoded_assets": [
    {
      "bytes": 228823,
      "sha256": "19090d56557bd5c0639fdd602220459ac345568495c017a2bc95c1f24ed5b15a",
      "format": "GIF",
      "width": 768,
      "height": 768,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 228823,
      "sha256": "19090d56557bd5c0639fdd602220459ac345568495c017a2bc95c1f24ed5b15a"
    }
  ]
}
```

## g41-size-1024x768

```json
{
  "case": "g41-size-1024x768",
  "source_result_sha256": "8e255faddcee557472e66763d7e4e5b08c727be1599b5304c5d48eb15f14200c",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x768",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 16.199,
  "decoded_assets": [
    {
      "bytes": 257217,
      "sha256": "13823262104ee666af125dd0379b92e215a6fc35af402294b06b0be8fd175811",
      "format": "GIF",
      "width": 1024,
      "height": 768,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 257217,
      "sha256": "13823262104ee666af125dd0379b92e215a6fc35af402294b06b0be8fd175811"
    }
  ]
}
```

## g42-size-768x1024

```json
{
  "case": "g42-size-768x1024",
  "source_result_sha256": "5feefd15cb084df062b4f6dfadeecf5a450e9a50dbfc646d7eb576c21cb33507",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "768x1024",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 16.488,
  "decoded_assets": [
    {
      "bytes": 426050,
      "sha256": "95349205f84c2cfc0168e767e140e68da4af9480f76bde49502f1aeefdfd0468",
      "format": "GIF",
      "width": 768,
      "height": 1024,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 426050,
      "sha256": "95349205f84c2cfc0168e767e140e68da4af9480f76bde49502f1aeefdfd0468"
    }
  ]
}
```

## g43-size-3840x2160

```json
{
  "case": "g43-size-3840x2160",
  "source_result_sha256": "b1c3f0f88d99c95d5afda049e8fc5200058145151acca1c6ed45e5fe388c4333",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "3840x2160",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 400,
  "elapsed_s": 0.371,
  "error": {
    "code": "invalid_value",
    "message": "GIF frames exceed the total request pixel budget. Reduce the frame size or frame_count.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g44-size-513x512

```json
{
  "case": "g44-size-513x512",
  "source_result_sha256": "40d2f9d9c00118fddf021d5fcabcfb5155810a90d82ad5959b8294775ea8a2af",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "513x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 400,
  "elapsed_s": 0.398,
  "error": {
    "code": "invalid_value",
    "message": "No supported sheet fits 4 GIF frames at size '513x512'. Invalid size '1026x2048'. Width and height must both be divisible by 16. Invalid size '2052x1024'. Width and height must both be divisible by 16.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g45-16frames-512

```json
{
  "case": "g45-16frames-512",
  "source_result_sha256": "3ee0f59eb7309ee2ce3aa74493bc54b1ef5480fd436a6102586c63ad4906097a",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 16,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 14.446,
  "decoded_assets": [
    {
      "bytes": 514782,
      "sha256": "f5984975af7bbcf907c9ee2afe6c925e070954ae790e66813461b2f4cac8363c",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 16,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 16,
      "duration_total_ms": 1600
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 514782,
      "sha256": "f5984975af7bbcf907c9ee2afe6c925e070954ae790e66813461b2f4cac8363c"
    }
  ]
}
```

## g46-9frames-768

```json
{
  "case": "g46-9frames-768",
  "source_result_sha256": "4d778fd38a07802d5fcfaa54219876812024c60b08c957896385c6ad582a7f8b",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "768x768",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 9,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 15.664,
  "decoded_assets": [
    {
      "bytes": 549915,
      "sha256": "9b7214d8ecd1a37f478694ee58be8c18fa1d51c09345d604eb10b78dba267a8e",
      "format": "GIF",
      "width": 768,
      "height": 768,
      "frames": 9,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 9,
      "duration_total_ms": 900
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 549915,
      "sha256": "9b7214d8ecd1a37f478694ee58be8c18fa1d51c09345d604eb10b78dba267a8e"
    }
  ]
}
```

## g47-stream

```json
{
  "case": "g47-stream",
  "source_result_sha256": "0a93f06574fb5f2554701eb100817832b7f322528694ae8070fd89399587fa17",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "stream": true
  },
  "http_status": 400,
  "elapsed_s": 0.35,
  "error": {
    "code": "invalid_value",
    "message": "'stream' is not supported for GIF output.",
    "param": "stream",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g48-count1

```json
{
  "case": "g48-count1",
  "source_result_sha256": "bd244dcc8d801912b8d993ec47edfc793868d4345c4a9a6129ceabe606f29c60",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 1
  },
  "http_status": 400,
  "elapsed_s": 0.425,
  "error": {
    "code": "invalid_value",
    "message": "Invalid 'frame_count' for GIF output. Received 1. Use an integer from 2 to 16, or omit 'frame_count' to use 9.",
    "param": "frame_count",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g49-n2

```json
{
  "case": "g49-n2",
  "source_result_sha256": "de054859a1695065beec147e11014c1c6334a4458d625c5a3405b8d5843ea7f3",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "n": 2,
    "output_format": "gif",
    "frame_count": 4
  },
  "http_status": 200,
  "elapsed_s": 10.026,
  "decoded_assets": [
    {
      "bytes": 144091,
      "sha256": "195854452737bcce303371cc690b87ddd1ee09de5b6cd58b8bdb4348f4a427a5",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        200,
        200,
        200,
        200
      ],
      "distinct_frames": 4,
      "duration_total_ms": 800
    },
    {
      "bytes": 121334,
      "sha256": "66b7cb0f2a139fc7e97191735f609910bc8b51784db81f9804abe799deb42346",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        200,
        200,
        200,
        200
      ],
      "distinct_frames": 4,
      "duration_total_ms": 800
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 144091,
      "sha256": "195854452737bcce303371cc690b87ddd1ee09de5b6cd58b8bdb4348f4a427a5"
    },
    {
      "filename": "output-1.gif",
      "bytes": 121334,
      "sha256": "66b7cb0f2a139fc7e97191735f609910bc8b51784db81f9804abe799deb42346"
    }
  ]
}
```

## g50-transparent

```json
{
  "case": "g50-transparent",
  "source_result_sha256": "490ec139fa4560fc5e51abe6f2c5e6fd1d50548780de5d676b912a8f852e4e5a",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "background": "transparent",
    "frame_count": 4
  },
  "http_status": 200,
  "elapsed_s": 11.721,
  "decoded_assets": [
    {
      "bytes": 157605,
      "sha256": "e185ef050dc992edd2430086c1e0a065f2c05b8c6c80addf197a196d39d3e485",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        200,
        200,
        200,
        200
      ],
      "distinct_frames": 4,
      "duration_total_ms": 800
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 157605,
      "sha256": "e185ef050dc992edd2430086c1e0a065f2c05b8c6c80addf197a196d39d3e485"
    }
  ]
}
```

## g60-input-jpg

```json
{
  "case": "g60-input-jpg",
  "source_result_sha256": "ec11e34473d2f2eaf50aad84b30c7c4517377dcb215b82381f9adec243c97bed",
  "endpoint": "/images/edits",
  "encoding": "multipart",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate the supplied orange robot waving its raised hand. Preserve its appearance. White background, fixed camera.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 9.487,
  "decoded_assets": [
    {
      "bytes": 93385,
      "sha256": "5e581207a1fb9ec09bf25d8f8fb59894f59b58c734b23ad0a85d1e5f4e04506e",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 93385,
      "sha256": "5e581207a1fb9ec09bf25d8f8fb59894f59b58c734b23ad0a85d1e5f4e04506e"
    }
  ]
}
```

## g60-input-webp

```json
{
  "case": "g60-input-webp",
  "source_result_sha256": "989ef4fa90ae2a0192892add65bc3dfdbe789fbef7cdd62039bfb6bdd5fb7ea4",
  "endpoint": "/images/edits",
  "encoding": "multipart",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Animate the supplied orange robot waving its raised hand. Preserve its appearance. White background, fixed camera.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 8.67,
  "decoded_assets": [
    {
      "bytes": 98574,
      "sha256": "910337ecb1da04074dcdc460e21d5831aedee9969a9f56347813437a1ba68053",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 98574,
      "sha256": "910337ecb1da04074dcdc460e21d5831aedee9969a9f56347813437a1ba68053"
    }
  ]
}
```

## g62-gif-input

```json
{
  "case": "g62-gif-input",
  "source_result_sha256": "5565dbb69daa220519fb78d0f5d3c1f6b95d52f198aaa71f87ae2b8c476b29b5",
  "endpoint": "/images/edits",
  "encoding": "multipart",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Make this robot blink its eyes. Preserve its appearance.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4
  },
  "http_status": 400,
  "elapsed_s": 0.556,
  "error": {
    "code": "unsupported_file_mimetype",
    "message": "Invalid file 'image': unsupported mimetype ('image/gif'). Supported file formats are 'image/jpeg', 'image/png', and 'image/webp'.",
    "param": "image",
    "type": "invalid_request_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g63-svg-input

```json
{
  "case": "g63-svg-input",
  "source_result_sha256": "5242cad39d5e264f2a0527c2d259184465a0f1c93ddadc1b00c1be32ea8b22f7",
  "endpoint": "/images/edits",
  "encoding": "multipart",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Make this robot blink its eyes. Preserve its appearance.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4
  },
  "http_status": 400,
  "elapsed_s": 0.447,
  "error": {
    "code": "unsupported_file_mimetype",
    "message": "Invalid file 'image': unsupported mimetype ('image/svg+xml'). Supported file formats are 'image/jpeg', 'image/png', and 'image/webp'.",
    "param": "image",
    "type": "invalid_request_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g64-sunburst-edit

```json
{
  "case": "g64-sunburst-edit",
  "source_result_sha256": "c012f8d125ccf8e8853e12c60152f642c17fd99db6761a1e3bd76c79874407e7",
  "endpoint": "/images/edits",
  "encoding": "multipart",
  "request": {
    "model": "gpt-image-2.6-sunburst",
    "prompt": "Animate the supplied orange robot waving its raised hand. Preserve its appearance. White background, fixed camera.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 14.997,
  "decoded_assets": [
    {
      "bytes": 77744,
      "sha256": "4ab894dc6bfcb9347db26de6c7dc0daabc210421686ca42e04345e3ab768a51d",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 77744,
      "sha256": "4ab894dc6bfcb9347db26de6c7dc0daabc210421686ca42e04345e3ab768a51d"
    }
  ]
}
```

## g70-9frames-960

```json
{
  "case": "g70-9frames-960",
  "source_result_sha256": "defe2d065c7c01ee494d4102dfa8aecb7194a6c4b3e3d7b49766cb18b2ad472c",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "960x960",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 9,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 17.98,
  "decoded_assets": [
    {
      "bytes": 636683,
      "sha256": "036e63bd5c6d8092dbc971a78888575bfaaa9aea27f0f372fb3db1b69c3a533e",
      "format": "GIF",
      "width": 960,
      "height": 960,
      "frames": 9,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 9,
      "duration_total_ms": 900
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 636683,
      "sha256": "036e63bd5c6d8092dbc971a78888575bfaaa9aea27f0f372fb3db1b69c3a533e"
    }
  ]
}
```

## g71-16frames-736

```json
{
  "case": "g71-16frames-736",
  "source_result_sha256": "68ef3c5da7091e0877198b8e69930f454e08ab223a4c42b74164559d8ded2b69",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "736x736",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 16,
    "frame_duration_ms": 100
  },
  "http_status": 400,
  "elapsed_s": 0.377,
  "error": {
    "code": "invalid_value",
    "message": "No supported sheet fits 16 GIF frames at size '736x736'. Invalid size '2944x2944'. Requested resolution exceeds the current pixel budget.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g72-16frames-n2

```json
{
  "case": "g72-16frames-n2",
  "source_result_sha256": "1e09c062a17d36d92892e90fb12bcf7741aaa8741a4f30663d66e9f7d410d71e",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "n": 2,
    "output_format": "gif",
    "frame_count": 16
  },
  "http_status": 200,
  "elapsed_s": 15.103,
  "decoded_assets": [
    {
      "bytes": 449424,
      "sha256": "3d7047154cd6c294c76bbf4f1705a2f1a7b87f394c206073ca1629b3352286c3",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 16,
      "loop": 0,
      "durations_ms": [
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200
      ],
      "distinct_frames": 16,
      "duration_total_ms": 3200
    },
    {
      "bytes": 443359,
      "sha256": "77ec342f6adf5ccb066a3cadc386c6efd53871be85136f1b11642e991c23db32",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 16,
      "loop": 0,
      "durations_ms": [
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200,
        200
      ],
      "distinct_frames": 16,
      "duration_total_ms": 3200
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 449424,
      "sha256": "3d7047154cd6c294c76bbf4f1705a2f1a7b87f394c206073ca1629b3352286c3"
    },
    {
      "filename": "output-1.gif",
      "bytes": 443359,
      "sha256": "77ec342f6adf5ccb066a3cadc386c6efd53871be85136f1b11642e991c23db32"
    }
  ]
}
```

## g73-gif-compression

```json
{
  "case": "g73-gif-compression",
  "source_result_sha256": "eb0e6b6aeaac38fa9930074ea659dece96c6537f93e146630d43d99dc334c3cf",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "output_compression": 80
  },
  "http_status": 400,
  "elapsed_s": 0.364,
  "error": {
    "code": "invalid_value",
    "message": "'output_compression' is not supported for GIF output.",
    "param": "output_compression",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g74-gif-partial

```json
{
  "case": "g74-gif-partial",
  "source_result_sha256": "adead92aa09334a37ae1ea6b71514cc713fdcdbd2c80496e6f7f4f3699959b0b",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "partial_images": 1
  },
  "http_status": 400,
  "elapsed_s": 0.363,
  "error": {
    "code": "unsupported_parameter",
    "message": "Partial images are only supported with streaming.",
    "param": "input",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g75-existing-gif-extracted-frame-edit

```json
{
  "case": "g75-existing-gif-extracted-frame-edit",
  "source_result_sha256": "635c16da28d9573f5a729f85c0694823dfb919d8fdd0e549bdd68323be2d14b9",
  "endpoint": "/images/edits",
  "encoding": "multipart",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Make the orange robot wave its raised hand. Change both eyes to green, keep everything else the same. Seamless loop, white background.",
    "size": "512x512",
    "quality": "low",
    "output_format": "gif",
    "frame_count": 4,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 10.705,
  "decoded_assets": [
    {
      "bytes": 185062,
      "sha256": "1a4031f0ca8af673ed27ef4d5d5489bc98a979883f1d096a7cf69d6027e3d2fa",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 4,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 4,
      "duration_total_ms": 400
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 185062,
      "sha256": "1a4031f0ca8af673ed27ef4d5d5489bc98a979883f1d096a7cf69d6027e3d2fa"
    }
  ]
}
```

## g80-requested-native4k-16f-50fps

```json
{
  "case": "g80-requested-native4k-16f-50fps",
  "source_result_sha256": "be96ae69e5f988fd852e9578b9e01e4c22df0daf25695601a9cf1cac7e162a36",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "A seamless looping animated GIF, cinematic 16:9 wide composition. A cute orange toy robot stands on a glossy deep navy platform, waves its raised right hand rapidly, while a luminous cyan ring rotates smoothly behind it. Bright orange and cyan, crisp geometric forms, polished 3D studio lighting. Fixed camera, no zoom, no cuts, no text. Keep robot appearance stable across frames. The motion should complete exactly one seamless cycle over all 16 frames.",
    "size": "3840x2160",
    "quality": "high",
    "output_format": "gif",
    "frame_count": 16,
    "frame_duration_ms": 20
  },
  "http_status": 400,
  "elapsed_s": 2.478,
  "error": {
    "code": "invalid_value",
    "message": "GIF frames exceed the total request pixel budget. Reduce the frame size or frame_count.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g81-4k-source-768x432-16f-50fps

```json
{
  "case": "g81-4k-source-768x432-16f-50fps",
  "source_result_sha256": "3c0aa2e9997486dcd4d49e871e530fca3c3b06891d5ca20512b154b9fcef86a9",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "A seamless looping animated GIF, cinematic 16:9 wide composition. A cute orange toy robot stands on a glossy deep navy platform, waves its raised right hand rapidly, while a luminous cyan ring rotates smoothly behind it. Bright orange and cyan, crisp geometric forms, polished 3D studio lighting. Fixed camera, no zoom, no cuts, no text. Keep robot appearance stable across frames. The motion should complete exactly one seamless cycle over all 16 frames.",
    "size": "768x432",
    "quality": "high",
    "output_format": "gif",
    "frame_count": 16,
    "frame_duration_ms": 20
  },
  "http_status": 200,
  "elapsed_s": 16.33,
  "decoded_assets": [
    {
      "bytes": 1198715,
      "sha256": "72be089b962aed52c2b2b6dc0ecf9b1b85d8a5c329348edc955e69041457d618",
      "format": "GIF",
      "width": 768,
      "height": 432,
      "frames": 16,
      "loop": 0,
      "durations_ms": [
        20,
        20,
        20,
        20,
        20,
        20,
        20,
        20,
        20,
        20,
        20,
        20,
        20,
        20,
        20,
        20
      ],
      "distinct_frames": 16,
      "duration_total_ms": 320
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 1198715,
      "sha256": "72be089b962aed52c2b2b6dc0ecf9b1b85d8a5c329348edc955e69041457d618"
    }
  ]
}
```

## g90-total-960-9frames-n10

```json
{
  "case": "g90-total-960-9frames-n10",
  "source_result_sha256": "ef9b4e13d667eb4d6d5405174b097b3bfd53cbe6ad3eb34cc4e811a79db5386f",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "A simple blue circle moving in a small circle on white background. Seamless loop.",
    "size": "960x960",
    "quality": "low",
    "n": 10,
    "output_format": "gif",
    "frame_count": 9,
    "frame_duration_ms": 100
  },
  "http_status": 400,
  "elapsed_s": 0.729,
  "error": {
    "code": "invalid_value",
    "message": "No supported sheet fits 9 GIF frames at size '960x960'. Sheet 2880x2880 exceeds the total request pixel budget for n=10.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g90-total-960-9frames-n2

```json
{
  "case": "g90-total-960-9frames-n2",
  "source_result_sha256": "6a648f33d13f665c49cc41654c1d139db138fb8de089bd872598c8fd82363e85",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "A simple blue circle moving in a small circle on white background. Seamless loop.",
    "size": "960x960",
    "quality": "low",
    "n": 2,
    "output_format": "gif",
    "frame_count": 9,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 15.218,
  "decoded_assets": [
    {
      "bytes": 133748,
      "sha256": "28260267d65631888ba659e8d1b76a0deeed6fba0d72575b592ad97932398c0e",
      "format": "GIF",
      "width": 960,
      "height": 960,
      "frames": 9,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 9,
      "duration_total_ms": 900
    },
    {
      "bytes": 180216,
      "sha256": "28a574d8a2eb85aa7918a3d77043db6a51b7becff95b90a18efe00049f21122e",
      "format": "GIF",
      "width": 960,
      "height": 960,
      "frames": 9,
      "loop": 0,
      "durations_ms": [
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100,
        100
      ],
      "distinct_frames": 9,
      "duration_total_ms": 900
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 133748,
      "sha256": "28260267d65631888ba659e8d1b76a0deeed6fba0d72575b592ad97932398c0e"
    },
    {
      "filename": "output-1.gif",
      "bytes": 180216,
      "sha256": "28a574d8a2eb85aa7918a3d77043db6a51b7becff95b90a18efe00049f21122e"
    }
  ]
}
```

## g90-total-960-9frames-n3

```json
{
  "case": "g90-total-960-9frames-n3",
  "source_result_sha256": "45091eb88e1091e940408c09e2de16dc7a3767e77f7ccc1a7278515ff8f5eac6",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "A simple blue circle moving in a small circle on white background. Seamless loop.",
    "size": "960x960",
    "quality": "low",
    "n": 3,
    "output_format": "gif",
    "frame_count": 9,
    "frame_duration_ms": 100
  },
  "http_status": 400,
  "elapsed_s": 2.095,
  "error": {
    "code": "invalid_value",
    "message": "No supported sheet fits 9 GIF frames at size '960x960'. Sheet 2880x2880 exceeds the total request pixel budget for n=3.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g91-total512-2frames-n8

```json
{
  "case": "g91-total512-2frames-n8",
  "source_result_sha256": "9613d2973483848f4dd7300510ece75014836e5edc7570e1268fb126ee78bf44",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Two frames: a blue dot moves a small distance on white.",
    "size": "512x512",
    "quality": "low",
    "n": 8,
    "output_format": "gif",
    "frame_count": 2,
    "frame_duration_ms": 100
  },
  "http_status": 200,
  "elapsed_s": 11.511,
  "decoded_assets": [
    {
      "bytes": 31621,
      "sha256": "e625667936d83f6c06659f9b90df058010f92a8e957d5f152968928b6b805792",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 2,
      "loop": 0,
      "durations_ms": [
        100,
        100
      ],
      "distinct_frames": 2,
      "duration_total_ms": 200
    },
    {
      "bytes": 35072,
      "sha256": "4db6a95249a4bc017ec38d485c5baeb8267fa0cf4dff50a72a382f34d9c98d22",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 2,
      "loop": 0,
      "durations_ms": [
        100,
        100
      ],
      "distinct_frames": 2,
      "duration_total_ms": 200
    },
    {
      "bytes": 36261,
      "sha256": "7e42c7cdbdd0ad02b9f5924ddc05695f342a919edf1556503b848d1933c0b352",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 2,
      "loop": 0,
      "durations_ms": [
        100,
        100
      ],
      "distinct_frames": 2,
      "duration_total_ms": 200
    },
    {
      "bytes": 37592,
      "sha256": "acc3f905dbe8be03682bdebbad1049b2d1a10bc46517709ce709ce3b228adf31",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 2,
      "loop": 0,
      "durations_ms": [
        100,
        100
      ],
      "distinct_frames": 2,
      "duration_total_ms": 200
    },
    {
      "bytes": 34014,
      "sha256": "27eff2156c318521c6365ceac85e77b0ba437b2fb04d5703f823770f91e64752",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 2,
      "loop": 0,
      "durations_ms": [
        100,
        100
      ],
      "distinct_frames": 2,
      "duration_total_ms": 200
    },
    {
      "bytes": 32158,
      "sha256": "5f43c2ab63552356f6292f4fd9cae761c8d729ecafa6670f8cfe9aa38bb9552f",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 2,
      "loop": 0,
      "durations_ms": [
        100,
        100
      ],
      "distinct_frames": 2,
      "duration_total_ms": 200
    },
    {
      "bytes": 29192,
      "sha256": "f55fdd23a48bc79ed0240cbad68a8c719ab308c67329b239c1ddf89997cb68f3",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 2,
      "loop": 0,
      "durations_ms": [
        100,
        100
      ],
      "distinct_frames": 2,
      "duration_total_ms": 200
    },
    {
      "bytes": 31603,
      "sha256": "ef26e9c47e8ceb50e79118ff8352a766130ab9e63c8c388d753d3057fd8535c6",
      "format": "GIF",
      "width": 512,
      "height": 512,
      "frames": 2,
      "loop": 0,
      "durations_ms": [
        100,
        100
      ],
      "distinct_frames": 2,
      "duration_total_ms": 200
    }
  ],
  "media_files": [
    {
      "filename": "output-0.gif",
      "bytes": 31621,
      "sha256": "e625667936d83f6c06659f9b90df058010f92a8e957d5f152968928b6b805792"
    },
    {
      "filename": "output-1.gif",
      "bytes": 35072,
      "sha256": "4db6a95249a4bc017ec38d485c5baeb8267fa0cf4dff50a72a382f34d9c98d22"
    },
    {
      "filename": "output-2.gif",
      "bytes": 36261,
      "sha256": "7e42c7cdbdd0ad02b9f5924ddc05695f342a919edf1556503b848d1933c0b352"
    },
    {
      "filename": "output-3.gif",
      "bytes": 37592,
      "sha256": "acc3f905dbe8be03682bdebbad1049b2d1a10bc46517709ce709ce3b228adf31"
    },
    {
      "filename": "output-4.gif",
      "bytes": 34014,
      "sha256": "27eff2156c318521c6365ceac85e77b0ba437b2fb04d5703f823770f91e64752"
    },
    {
      "filename": "output-5.gif",
      "bytes": 32158,
      "sha256": "5f43c2ab63552356f6292f4fd9cae761c8d729ecafa6670f8cfe9aa38bb9552f"
    },
    {
      "filename": "output-6.gif",
      "bytes": 29192,
      "sha256": "f55fdd23a48bc79ed0240cbad68a8c719ab308c67329b239c1ddf89997cb68f3"
    },
    {
      "filename": "output-7.gif",
      "bytes": 31603,
      "sha256": "ef26e9c47e8ceb50e79118ff8352a766130ab9e63c8c388d753d3057fd8535c6"
    }
  ]
}
```

## g91-total512-2frames-n9

```json
{
  "case": "g91-total512-2frames-n9",
  "source_result_sha256": "3b7b78f4e93e6febe497a4b9db83257355f3b8fe98e3e547792a7d1153276312",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Two frames: a blue dot moves a small distance on white.",
    "size": "512x512",
    "quality": "low",
    "n": 9,
    "output_format": "gif",
    "frame_count": 2,
    "frame_duration_ms": 100
  },
  "http_status": 400,
  "elapsed_s": 0.384,
  "error": {
    "code": "invalid_value",
    "message": "No supported sheet fits 2 GIF frames at size '512x512'. Sheet 2048x1024 exceeds the total request pixel budget for n=9. Sheet 1024x2048 exceeds the total request pixel budget for n=9.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## g93-total-just-above-16MiP

```json
{
  "case": "g93-total-just-above-16MiP",
  "source_result_sha256": "954c3c6ef7917928e310f99f09f120da9dbc299d2a143aa3a2231f1a2ee3c204",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "A blue dot moving on a white background.",
    "size": "496x752",
    "quality": "low",
    "n": 5,
    "output_format": "gif",
    "frame_count": 9
  },
  "http_status": 400,
  "elapsed_s": 0.423,
  "error": {
    "code": "invalid_value",
    "message": "No supported sheet fits 9 GIF frames at size '496x752'. Sheet 1488x2256 exceeds the total request pixel budget for n=5.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## r01-raster-1024x1024

```json
{
  "case": "r01-raster-1024x1024",
  "source_result_sha256": "e03b2420141f85b40e1c62cf6113bd73054ad5064c3eac0916d3b795e7ebaaf7",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "png"
  },
  "http_status": 200,
  "elapsed_s": 6.634,
  "decoded_assets": [
    {
      "bytes": 747751,
      "sha256": "ff1a066b1164cfb070a8670181ab8e4926468dc92cb588df4965a81329f1eb22",
      "format": "PNG",
      "width": 1024,
      "height": 1024,
      "frames": 1,
      "loop": null,
      "durations_ms": [
        null
      ],
      "distinct_frames": 1,
      "duration_total_ms": 0
    }
  ],
  "media_files": [
    {
      "filename": "output-0.png",
      "bytes": 747751,
      "sha256": "ff1a066b1164cfb070a8670181ab8e4926468dc92cb588df4965a81329f1eb22"
    }
  ]
}
```

## r02-raster-1536x864

```json
{
  "case": "r02-raster-1536x864",
  "source_result_sha256": "1c72e4f151253545697ce9503564cb9b606b9ffec2878f17f961558ec96ed83e",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1536x864",
    "quality": "low",
    "output_format": "png"
  },
  "http_status": 200,
  "elapsed_s": 6.969,
  "decoded_assets": [
    {
      "bytes": 956411,
      "sha256": "95f1ec0e78214ba2e5eb338011b214aa72a8d5b375614db52224ade80343ad7e",
      "format": "PNG",
      "width": 1536,
      "height": 864,
      "frames": 1,
      "loop": null,
      "durations_ms": [
        null
      ],
      "distinct_frames": 1,
      "duration_total_ms": 0
    }
  ],
  "media_files": [
    {
      "filename": "output-0.png",
      "bytes": 956411,
      "sha256": "95f1ec0e78214ba2e5eb338011b214aa72a8d5b375614db52224ade80343ad7e"
    }
  ]
}
```

## r03-raster-864x1536

```json
{
  "case": "r03-raster-864x1536",
  "source_result_sha256": "2f0fb19c6a6c1d0eb4530c636dbd12de20390308e0dcfa4a36a94787213cd171",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "864x1536",
    "quality": "low",
    "output_format": "png"
  },
  "http_status": 200,
  "elapsed_s": 6.1,
  "decoded_assets": [
    {
      "bytes": 919956,
      "sha256": "72cf00f0f668a147501a2df05124a8199b50e76a050fae3479125a6424326b51",
      "format": "PNG",
      "width": 864,
      "height": 1536,
      "frames": 1,
      "loop": null,
      "durations_ms": [
        null
      ],
      "distinct_frames": 1,
      "duration_total_ms": 0
    }
  ],
  "media_files": [
    {
      "filename": "output-0.png",
      "bytes": 919956,
      "sha256": "72cf00f0f668a147501a2df05124a8199b50e76a050fae3479125a6424326b51"
    }
  ]
}
```

## r04-raster-3840x2160

```json
{
  "case": "r04-raster-3840x2160",
  "source_result_sha256": "2a39856fc288c800d0867999da29ef89a2bfbcc9352e570f467352af502bb20e",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "3840x2160",
    "quality": "low",
    "output_format": "png"
  },
  "http_status": 200,
  "elapsed_s": 15.263,
  "decoded_assets": [
    {
      "bytes": 3383032,
      "sha256": "2af8c4999a0a2dd0763ab9a446bfa6c49df3a6a74250d3b755bdfac21589d6ae",
      "format": "PNG",
      "width": 3840,
      "height": 2160,
      "frames": 1,
      "loop": null,
      "durations_ms": [
        null
      ],
      "distinct_frames": 1,
      "duration_total_ms": 0
    }
  ],
  "media_files": [
    {
      "filename": "output-0.png",
      "bytes": 3383032,
      "sha256": "2af8c4999a0a2dd0763ab9a446bfa6c49df3a6a74250d3b755bdfac21589d6ae"
    }
  ]
}
```

## r05-raster-512x512

```json
{
  "case": "r05-raster-512x512",
  "source_result_sha256": "1aca42b7a2b8d94ba383459654e40b6809762d72834c16525d51f908683073a7",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "png"
  },
  "http_status": 400,
  "elapsed_s": 0.723,
  "error": {
    "code": "invalid_value",
    "message": "Invalid size '512x512'. Requested resolution is below the current minimum pixel budget.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## r10-transparent-jpeg

```json
{
  "case": "r10-transparent-jpeg",
  "source_result_sha256": "c0fb9cc80bc712f75c4d98ca7f6d2d5c9d006073589170cc15fc079eece64cf4",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "jpeg",
    "background": "transparent"
  },
  "http_status": 400,
  "elapsed_s": 1.117,
  "error": {
    "code": "invalid_transparent_background_output_format",
    "message": "Transparent background is not supported for JPEG output format",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## r10-transparent-png

```json
{
  "case": "r10-transparent-png",
  "source_result_sha256": "0d46b4ca66a3e5b95aa227e85326e2ba89d480952944ad9df1ee70a23399d936",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "png",
    "background": "transparent"
  },
  "http_status": 200,
  "elapsed_s": 6.583,
  "decoded_assets": [
    {
      "bytes": 932634,
      "sha256": "8946356f25e6a1c871882816b730146b2e8edf9814432d4dd5b86c63f700597c",
      "format": "PNG",
      "width": 1024,
      "height": 1024,
      "frames": 1,
      "loop": null,
      "durations_ms": [
        null
      ],
      "distinct_frames": 1,
      "duration_total_ms": 0
    }
  ],
  "media_files": [
    {
      "filename": "output-0.png",
      "bytes": 932634,
      "sha256": "8946356f25e6a1c871882816b730146b2e8edf9814432d4dd5b86c63f700597c"
    }
  ]
}
```

## r10-transparent-webp

```json
{
  "case": "r10-transparent-webp",
  "source_result_sha256": "db49398318c3605332b8a0624a7b983e31fe1ba46ef9e05c8fb74a86f375345f",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "webp",
    "background": "transparent"
  },
  "http_status": 200,
  "elapsed_s": 6.988,
  "decoded_assets": [
    {
      "bytes": 342264,
      "sha256": "efa479033ac4bb2e704cd306c4a5e7d381208453ee20b917a5200d58e52e6f6c",
      "format": "WEBP",
      "width": 1024,
      "height": 1024,
      "frames": 1,
      "loop": 1,
      "durations_ms": [
        0
      ],
      "distinct_frames": 1,
      "duration_total_ms": 0
    }
  ],
  "media_files": [
    {
      "filename": "output-0.webp",
      "bytes": 342264,
      "sha256": "efa479033ac4bb2e704cd306c4a5e7d381208453ee20b917a5200d58e52e6f6c"
    }
  ]
}
```

## s01-flare-svg-default

```json
{
  "case": "s01-flare-svg-default",
  "source_result_sha256": "facfe5e4c3cd02906d03c0e7abf8d521ffea8b613af216fff832f4a0d5156fc8",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "n": 1,
    "output_format": "svg"
  },
  "http_status": 400,
  "elapsed_s": 2.587,
  "error": {
    "code": "missing_required_parameter",
    "message": "SVG generation requires 'svg.model'.",
    "param": "svg.model",
    "type": "invalid_request_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## s02-sunburst-svg

```json
{
  "case": "s02-sunburst-svg",
  "source_result_sha256": "186532f759e3a2188263a6acd58f914685b3fb36760b7e2173f85f8544c6e246",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-sunburst",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "n": 1,
    "output_format": "svg"
  },
  "http_status": 400,
  "elapsed_s": 2.39,
  "error": {
    "code": "missing_required_parameter",
    "message": "SVG generation requires 'svg.model'.",
    "param": "svg.model",
    "type": "invalid_request_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## s03-svg-author-sol

```json
{
  "case": "s03-svg-author-sol",
  "source_result_sha256": "93c18c710d3351044385b6953c2a661ef449e6cc8c000e2275df59612c157741",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "n": 1,
    "output_format": "svg",
    "svg": {
      "model": "gpt-6.1-sol"
    }
  },
  "http_status": 200,
  "elapsed_s": 79.401,
  "decoded_assets": [
    {
      "bytes": 6649,
      "sha256": "7d128964d90b378dd96b50a0820cfec17701c7794530bdef32e35fec2c427f2d",
      "format": "SVG",
      "width": "1024",
      "height": "1024"
    }
  ],
  "media_files": [
    {
      "filename": "output-0-preview.png",
      "bytes": 49529,
      "sha256": "0f2c3729c35b6fb99a4c8420e607584630066fdaa5413b658af94ee0ba319d40"
    },
    {
      "filename": "output-0.svg",
      "bytes": 6649,
      "sha256": "7d128964d90b378dd96b50a0820cfec17701c7794530bdef32e35fec2c427f2d"
    }
  ]
}
```

## s04-edit-multipart-svg

```json
{
  "case": "s04-edit-multipart-svg",
  "source_result_sha256": "4dea561cc3dcc9892c067a82975d6da01b18ba21321def72d542236b532f0042",
  "endpoint": "/images/edits",
  "encoding": "multipart",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Convert the supplied orange robot into clean editable SVG vector paths. Preserve its geometry, colors and raised hand. No raster embedding.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "svg"
  },
  "http_status": 400,
  "elapsed_s": 0.371,
  "error": {
    "code": "invalid_value",
    "message": "Invalid value: 'svg'. Supported values are: 'png', 'webp', 'jpeg', and 'gif'.",
    "param": "output_format",
    "type": "invalid_request_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## s05-edit-json-svg

```json
{
  "case": "s05-edit-json-svg",
  "source_result_sha256": "7766103313456dae077514da287f8bc9bd703a587f0a6d3b3bbf68380f373876",
  "endpoint": "/images/edits",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Vectorize this orange robot as editable SVG paths. Preserve geometry and colors.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "svg"
  },
  "http_status": 400,
  "elapsed_s": 0.453,
  "error": {
    "code": "invalid_value",
    "message": "Invalid value: 'svg'. Supported values are: 'png', 'webp', 'jpeg', and 'gif'.",
    "param": "output_format",
    "type": "invalid_request_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## s06-sunburst-author

```json
{
  "case": "s06-sunburst-author",
  "source_result_sha256": "f9efcc2db40f6418c67a789edab8ff68e1628d4b08a78415ee69b0be0b66cbb8",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-sunburst",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1536x864",
    "quality": "low",
    "n": 1,
    "output_format": "svg",
    "svg": {
      "model": "gpt-6.1-sol"
    }
  },
  "http_status": 200,
  "elapsed_s": 99.009,
  "decoded_assets": [
    {
      "bytes": 6105,
      "sha256": "2c6b754444f0da227a9bf9c1bc1e86444011f06b84017823e12bd3a0569fd0b6",
      "format": "SVG",
      "width": "1536",
      "height": "864"
    }
  ],
  "media_files": [
    {
      "filename": "output-0-preview.png",
      "bytes": 31962,
      "sha256": "886aa0bf9858a27384a5ecb01539a4c346113314710e5abe8b2abec93898b5e4"
    },
    {
      "filename": "output-0.svg",
      "bytes": 6105,
      "sha256": "2c6b754444f0da227a9bf9c1bc1e86444011f06b84017823e12bd3a0569fd0b6"
    }
  ]
}
```

## s07-svg-n2

```json
{
  "case": "s07-svg-n2",
  "source_result_sha256": "271a478996ff722c8dc9befe0adede1f24f04f98d866b17c8ffa4e12b3a36051",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "n": 2,
    "output_format": "svg",
    "svg": {
      "model": "gpt-6.1-sol"
    }
  },
  "http_status": 400,
  "elapsed_s": 1.126,
  "error": {
    "code": "invalid_value",
    "message": "The 'n' parameter is not supported for SVG generation. Use n=1.",
    "param": "n",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## s08-svg-stream

```json
{
  "case": "s08-svg-stream",
  "source_result_sha256": "0263f7953f9b44f1dece581ad562d8d02964986b77963016fc76121248dfa9a7",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "svg",
    "stream": true,
    "svg": {
      "model": "gpt-6.1-sol"
    }
  },
  "http_status": 400,
  "elapsed_s": 0.397,
  "error": {
    "code": "invalid_value",
    "message": "The 'stream' parameter is not supported for SVG generation.",
    "param": "stream",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## s09-svg-edit-with-author

```json
{
  "case": "s09-svg-edit-with-author",
  "source_result_sha256": "8441c9f3f12a3d204e77e1bdb1aac1fe8ea2cf641225539dcf14a44e124376c6",
  "endpoint": "/images/edits",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "svg",
    "svg": {
      "model": "gpt-6.1-sol"
    }
  },
  "http_status": 400,
  "elapsed_s": 0.41,
  "error": {
    "code": "unknown_parameter",
    "message": "Unknown parameter: 'svg'.",
    "param": "svg",
    "type": "invalid_request_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## s10-svg-generations-with-images

```json
{
  "case": "s10-svg-generations-with-images",
  "source_result_sha256": "edc6176283408f1479fb06bc345ba1329700ee34b067882bf952f0b33f0ed8ff",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Convert the provided reference image into an editable SVG vector document. Preserve its exact colors and shape.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "svg",
    "svg": {
      "model": "gpt-6.1-sol"
    }
  },
  "http_status": 400,
  "elapsed_s": 0.37,
  "error": {
    "code": "unknown_parameter",
    "message": "Unknown parameter: 'images'.",
    "param": "images",
    "type": "invalid_request_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## s11-vision-to-svg-alternative

```json
{
  "case": "s11-vision-to-svg-alternative",
  "source_result_sha256": "b1cb9e5fc0f36c07ad78db8220e286d5599726e602a935c3a2620d027236542b",
  "endpoint": "/responses",
  "encoding": "json",
  "request": {
    "model": "gpt-6.1-sol",
    "store": false,
    "reasoning": {
      "effort": "low"
    },
    "input_text": [
      "Convert this input image to a faithful editable static SVG. Match its orange rounded body, blue round eyes, dark mouth, right raised hand and white background. Return ONLY a complete SVG document with viewBox 0 0 1024 1024. Use vector primitives and paths, no embedded raster, external resources or scripts. Do not call any tools."
    ],
    "has_reference_image": true
  },
  "http_status": 200,
  "elapsed_s": 6.551,
  "decoded_assets": [],
  "media_files": [
    {
      "filename": "output-0-preview.png",
      "bytes": 6114,
      "sha256": "58d0f0220ab9af5251e9b06646f24406351b49d8b13d1671d115b04629a84d85"
    },
    {
      "filename": "output-0.svg",
      "bytes": 502,
      "sha256": "d1848e048420be55f9aa882f2c5afec703be44fae68c59b2207a06b51c18a677"
    }
  ]
}
```

## s12-svg-size512

```json
{
  "case": "s12-svg-size512",
  "source_result_sha256": "e50c2d8b76a34c0a3e2d09c61f45592671d09122b6bfc9cf5634b799c51ffa51",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "512x512",
    "quality": "low",
    "output_format": "svg",
    "svg": {
      "model": "gpt-6.1-sol"
    }
  },
  "http_status": 400,
  "elapsed_s": 0.561,
  "error": {
    "code": "invalid_value",
    "message": "Invalid size '512x512'. Requested resolution is below the current minimum pixel budget.",
    "param": "size",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```

## s13-svg-compression

```json
{
  "case": "s13-svg-compression",
  "source_result_sha256": "e69fd17903e9c9e9db6e864b3d1093d3096dc8734bb50cbd6e491044eca1084e",
  "endpoint": "/images/generations",
  "encoding": "json",
  "request": {
    "model": "gpt-image-2.6-flare",
    "prompt": "Flat vector-style icon of an orange robot with two blue eyes waving its right hand. White background. Clean simple geometric shapes, no text.",
    "size": "1024x1024",
    "quality": "low",
    "output_format": "svg",
    "output_compression": 80,
    "svg": {
      "model": "gpt-6.1-sol"
    }
  },
  "http_status": 400,
  "elapsed_s": 0.383,
  "error": {
    "code": "invalid_value",
    "message": "The 'output_compression' parameter is not supported for SVG generation.",
    "param": "output_compression",
    "type": "image_generation_user_error"
  },
  "decoded_assets": [],
  "media_files": []
}
```
