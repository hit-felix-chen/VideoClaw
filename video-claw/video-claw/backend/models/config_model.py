"""Central model registry.

All backend model metadata is registered in this file, including provider,
model type, concurrency, pricing, and API capability tags.
"""

import os
import sys

models_dir = os.path.dirname(os.path.abspath(__file__))
backend_dir = os.path.dirname(models_dir)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from copy import deepcopy
from typing import Any, Optional

MODEL_CONFIG: dict[str, Any] = {'models': {'deepseek-chat': {'name': 'DeepSeek Chat',
                              'provider': 'deepseek',
                              'type': ['llm'],
                              'concurrency': 10,
                              'price_per_1k_input_token': 0.002,
                              'price_per_1k_output_token': 0.008},
            'deepseek-reasoner': {'name': 'DeepSeek Reasoner',
                                  'provider': 'deepseek',
                                  'type': ['llm'],
                                  'concurrency': 10,
                                  'price_per_1k_input_token': 0.004,
                                  'price_per_1k_output_token': 0.016},
            'deepseek-v4-flash': {'name': 'DeepSeek V4 Flash',
                                  'provider': 'deepseek',
                                  'type': ['llm'],
                                  'concurrency': 10,
                                  'price_per_1k_input_token': 0.001,
                                  'price_per_1k_output_token': 0.004},
            'deepseek-v4-pro': {'name': 'DeepSeek V4 Pro',
                                'provider': 'deepseek',
                                'type': ['llm'],
                                'concurrency': 10,
                                'price_per_1k_input_token': 0.002,
                                'price_per_1k_output_token': 0.008},
            'gpt-4o': {'name': 'GPT-4o',
                       'provider': 'openai',
                       'type': ['llm'],
                       'concurrency': 10,
                       'price_per_1k_input_token': 0.018,
                       'price_per_1k_output_token': 0.072},
            'gpt-5': {'name': 'GPT-5',
                      'provider': 'openai',
                      'type': ['llm'],
                      'concurrency': 10,
                      'price_per_1k_input_token': 0.02,
                      'price_per_1k_output_token': 0.1},
            'gpt-5.4': {'name': 'GPT-5.4',
                        'provider': 'openai',
                        'type': ['llm', 'vlm'],
                        'concurrency': 10,
                        'price_per_1k_input_token': 0.03,
                        'price_per_1k_output_token': 0.12},
            'qwen3.7-max': {'name': 'Qwen 3.7 Max',
                            'provider': 'dashscope',
                            'family': 'qwen',
                            'type': ['llm'],
                            'concurrency': 10,
                            'price_per_1k_input_token': 0.006,
                            'price_per_1k_output_token': 0.024},
            'qwen3.7-plus': {'name': 'Qwen 3.7 Plus',
                             'provider': 'dashscope',
                             'family': 'qwen',
                             'type': ['vlm'],
                             'concurrency': 10,
                             'price_per_1k_input_token': 0.0008,
                             'price_per_1k_output_token': 0.002},
            'qwen3.5-plus': {'name': 'Qwen 3.5 Plus',
                             'provider': 'dashscope',
                             'family': 'qwen',
                             'type': ['vlm'],
                             'concurrency': 10,
                             'price_per_1k_input_token': 0.0008,
                             'price_per_1k_output_token': 0.002},
            'qwen3.6-max-preview': {'name': 'Qwen 3.6 Max Preview',
                                    'provider': 'dashscope',
                                    'family': 'qwen',
                                    'type': ['llm'],
                                    'concurrency': 10,
                                    'price_per_1k_input_token': 0.006,
                                    'price_per_1k_output_token': 0.024},
            'qwen3-max': {'name': 'Qwen 3 Max',
                          'provider': 'dashscope',
                          'family': 'qwen',
                          'type': ['llm'],
                          'concurrency': 10,
                          'price_per_1k_input_token': 0.006,
                          'price_per_1k_output_token': 0.024},
            'deepseek-v3.2': {'name': 'DeepSeek V3.2 (DashScope)',
                              'provider': 'dashscope',
                              'family': 'deepseek',
                              'type': ['llm'],
                              'concurrency': 10,
                              'price_per_1k_input_token': 0.002,
                              'price_per_1k_output_token': 0.008},
            'qwen3.6-plus': {'name': 'Qwen 3.6 Plus',
                             'provider': 'dashscope',
                             'family': 'qwen',
                             'type': ['vlm'],
                             'concurrency': 10,
                             'price_per_1k_input_token': 0.0008,
                             'price_per_1k_output_token': 0.002},
            'qwen3.6-flash': {'name': 'Qwen 3.6 Flash',
                              'provider': 'dashscope',
                              'family': 'qwen',
                              'type': ['vlm'],
                              'concurrency': 10,
                              'price_per_1k_input_token': 0.0001,
                              'price_per_1k_output_token': 0.0001},
            'kimi-k2.6': {'name': 'Kimi K2.6',
                          'provider': 'dashscope',
                          'family': 'kimi',
                          'type': ['llm', 'vlm'],
                          'concurrency': 10,
                          'price_per_1k_input_token': 0.001,
                          'price_per_1k_output_token': 0.003},
            'gemini-2.5-flash': {'name': 'Gemini 2.5 Flash',
                                 'provider': 'gemini',
                                 'type': ['llm'],
                                 'concurrency': 10,
                                 'price_per_1k_input_token': 0.002,
                                 'price_per_1k_output_token': 0.01},
            'gemini-2.0-flash': {'name': 'Gemini 2.0 Flash',
                                 'provider': 'gemini',
                                 'type': ['llm', 'vlm'],
                                 'concurrency': 10,
                                 'price_per_1k_input_token': 0.002,
                                 'price_per_1k_output_token': 0.01},
            'gemini-2.5-flash-image': {'name': 'Gemini 2.5 Flash Image',
                                       'provider': 'gemini',
                                       'type': ['vlm'],
                                       'concurrency': 10,
                                       'price_per_1k_input_token': 0.002,
                                       'price_per_1k_output_token': 0.01},
            'wan2.7-image': {'name': 'Wan 2.7 Image',
                             'provider': 'dashscope',
                             'family': 'wan',
                             'type': ['t2i', 'i2i'],
                             'concurrency': 5,
                             'price_per_image': 0.2,
                             'capabilities': {'ability_type': 'image_generation',
                                              'ability_types': ['text_to_image', 'image_to_image', 'reference_image'],
                                              'adapter_ability_types': ['text_to_image',
                                                                        'image_to_image',
                                                                        'reference_image'],
                                              'input_modalities': ['text', 'image'],
                                              'adapter_input_modalities': ['text', 'image'],
                                              'api_contract_verified': True,
                                              'resolutions': ['720P', '1080P', '2K', '4K'],
                                              'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'wan2.7-image-pro': {'name': 'Wan 2.7 Image Pro',
                                 'provider': 'dashscope',
                                 'family': 'wan',
                                 'type': ['t2i', 'i2i'],
                                 'concurrency': 5,
                                 'price_per_image': 0.4,
                                 'capabilities': {'ability_type': 'image_generation',
                                                  'ability_types': ['text_to_image',
                                                                    'image_to_image',
                                                                    'reference_image',
                                                                    'high_quality'],
                                                  'adapter_ability_types': ['text_to_image',
                                                                            'image_to_image',
                                                                            'reference_image'],
                                                  'input_modalities': ['text', 'image'],
                                                  'adapter_input_modalities': ['text', 'image'],
                                                  'api_contract_verified': True,
                                                  'resolutions': ['720P', '1080P', '2K', '4K'],
                                                  'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'wan2.6-t2i': {'name': 'Wan 2.6 T2I',
                           'provider': 'dashscope',
                           'family': 'wan',
                           'type': ['t2i'],
                           'concurrency': 5,
                           'price_per_image': 0.2,
                           'capabilities': {'ability_type': 'image_generation',
                                            'ability_types': ['text_to_image'],
                                            'adapter_ability_types': ['text_to_image'],
                                            'input_modalities': ['text'],
                                            'adapter_input_modalities': ['text'],
                                            'api_contract_verified': True,
                                            'resolutions': ['720P', '1080P', '2K', '4K'],
                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'doubao-seedream-5-0-260128': {'name': 'Seedream 5.0',
                                           'provider': 'ark',
                                           'family': 'seedream',
                                           'type': ['t2i', 'i2i'],
                                           'concurrency': 10,
                                           'price_per_image': 0.22,
                                           'capabilities': {'ability_type': 'image_generation',
                                                            'ability_types': ['text_to_image',
                                                                              'image_to_image',
                                                                              'reference_image',
                                                                              'high_quality'],
                                                            'adapter_ability_types': ['text_to_image',
                                                                                      'image_to_image',
                                                                                      'reference_image'],
                                                            'input_modalities': ['text', 'image'],
                                                            'adapter_input_modalities': ['text', 'image'],
                                                            'api_contract_verified': True,
                                                            'resolutions': ['720P', '1080P', '2K', '4K'],
                                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'doubao-seedream-4-5-251128': {'name': 'Seedream 4.5',
                                           'provider': 'ark',
                                           'family': 'seedream',
                                           'type': ['t2i', 'i2i'],
                                           'concurrency': 10,
                                           'price_per_image': 0.25,
                                           'capabilities': {'ability_type': 'image_generation',
                                                            'ability_types': ['text_to_image',
                                                                              'image_to_image',
                                                                              'reference_image'],
                                                            'adapter_ability_types': ['text_to_image',
                                                                                      'image_to_image',
                                                                                      'reference_image'],
                                                            'input_modalities': ['text', 'image'],
                                                            'adapter_input_modalities': ['text', 'image'],
                                                            'api_contract_verified': True,
                                                            'resolutions': ['720P', '1080P', '2K', '4K'],
                                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'doubao-seedream-4-0-250828': {'name': 'Seedream 4.0',
                                           'provider': 'ark',
                                           'family': 'seedream',
                                           'type': ['t2i', 'i2i'],
                                           'concurrency': 10,
                                           'price_per_image': 0.2,
                                           'capabilities': {'ability_type': 'image_generation',
                                                            'ability_types': ['text_to_image',
                                                                              'image_to_image',
                                                                              'reference_image'],
                                                            'adapter_ability_types': ['text_to_image',
                                                                                      'image_to_image',
                                                                                      'reference_image'],
                                                            'input_modalities': ['text', 'image'],
                                                            'adapter_input_modalities': ['text', 'image'],
                                                            'api_contract_verified': True,
                                                            'resolutions': ['720P', '1080P', '2K', '4K'],
                                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4']}},
            'sora_image': {'name': 'Sora Image',
                           'provider': 'openai',
                           'type': ['t2i'],
                           'concurrency': 3,
                           'price_per_image': 1.45,
                           'capabilities': {'ability_type': 'image_generation',
                                            'ability_types': ['text_to_image'],
                                            'adapter_ability_types': ['text_to_image'],
                                            'input_modalities': ['text'],
                                            'adapter_input_modalities': ['text'],
                                            'api_contract_verified': True}},
            'gpt-image-2': {'name': 'GPT Image 2',
                            'provider': 'openai',
                            'type': ['t2i', 'i2i'],
                            'concurrency': 3,
                            'price_per_image': 1.09,
                            'capabilities': {'ability_type': 'image_generation',
                                             'ability_types': ['text_to_image', 'image_to_image', 'reference_image'],
                                             'adapter_ability_types': ['text_to_image',
                                                                       'image_to_image',
                                                                       'reference_image'],
                                             'input_modalities': ['text', 'image'],
                                             'adapter_input_modalities': ['text', 'image'],
                                             'api_contract_verified': True}},
            'wan2.6-i2v-flash': {'name': 'Wan 2.6 I2V Flash',
                                 'provider': 'dashscope',
                                 'family': 'wan',
                                 'type': ['video'],
                                 'concurrency': 5,
                                 'price_per_second': 0.6,
                                 'capabilities': {'ability_type': 'image_to_video',
                                                  'ability_types': ['first_frame_i2v',
                                                                    'audio_driven_i2v',
                                                                    'multi_shot',
                                                                    'fast_generation'],
                                                  'adapter_ability_types': ['first_frame_i2v'],
                                                  'input_modalities': ['text', 'image', 'audio'],
                                                  'adapter_input_modalities': ['text', 'image'],
                                                  'duration': {'min': 2, 'max': 15, 'integer': True, 'verified': True},
                                                  'resolutions': ['720P', '1080P'],
                                                  'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4'],
                                                  'api_contract_verified': True}},
            'wan2.7-i2v': {'name': 'Wan 2.7 I2V',
                           'provider': 'dashscope',
                           'family': 'wan',
                           'type': ['video'],
                           'concurrency': 5,
                           'price_per_second': 0.8,
                           'capabilities': {'ability_type': 'image_to_video',
                                            'ability_types': ['first_frame_i2v',
                                                              'start_end_frame_i2v',
                                                              'video_continuation',
                                                              'audio_driven_i2v',
                                                              'multi_shot'],
                                            'adapter_ability_types': ['first_frame_i2v',
                                                                      'start_end_frame_i2v',
                                                                      'audio_driven_i2v'],
                                            'input_modalities': ['text', 'image', 'audio', 'video'],
                                            'adapter_input_modalities': ['text', 'image'],
                                            'duration': {'min': 2, 'max': 15, 'integer': True, 'verified': True},
                                            'resolutions': ['720P', '1080P'],
                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4'],
                                            'api_contract_verified': True}},
            'happyhorse-1.0-i2v': {'name': 'Happy Horse 1.0 I2V',
                                   'provider': 'dashscope',
                                   'family': 'happyhorse',
                                   'type': ['video'],
                                   'concurrency': 5,
                                   'price_per_second': 1.0,
                                   'capabilities': {'ability_type': 'image_to_video',
                                                    'ability_types': ['first_frame_i2v', 'native_audio'],
                                                    'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                                    'input_modalities': ['text', 'image'],
                                                    'adapter_input_modalities': ['text', 'image'],
                                                    'duration': {'min': 3,
                                                                 'max': 15,
                                                                 'integer': True,
                                                                 'verified': True},
                                                    'resolutions': ['720P', '1080P'],
                                                    'api_contract_verified': True}},
            'happyhorse-1.1-i2v': {'name': 'Happy Horse 1.1 I2V',
                                   'provider': 'dashscope',
                                   'family': 'happyhorse',
                                   'type': ['video'],
                                   'concurrency': 5,
                                   'price_per_second': 1.0,
                                   'capabilities': {'ability_type': 'image_to_video',
                                                    'ability_types': ['first_frame_i2v', 'native_audio'],
                                                    'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                                    'input_modalities': ['text', 'image'],
                                                    'adapter_input_modalities': ['text', 'image'],
                                                    'duration': {'min': 3,
                                                                 'max': 15,
                                                                 'integer': True,
                                                                 'verified': True},
                                                    'resolutions': ['720P', '1080P'],
                                                    'api_contract_verified': True}},
            'kling-v3': {'name': 'Kling V3',
                         'provider': 'kling',
                         'type': ['video'],
                         'concurrency': 10,
                         'price_per_second': 1.0,
                         'capabilities': {'ability_type': 'image_to_video',
                                          'ability_types': ['text_to_video',
                                                            'image_to_video',
                                                            'start_end_frame_i2v',
                                                            'native_audio',
                                                            'multi_shot',
                                                            'element_reference'],
                                          'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                          'input_modalities': ['text', 'image'],
                                          'adapter_input_modalities': ['text', 'image'],
                                          'duration': {'min': 3, 'max': 15, 'integer': True, 'verified': True},
                                          'resolutions': ['720P', '1080P'],
                                          'api_contract_verified': True}},
            'kling-v2-6': {'name': 'Kling V2.6',
                           'provider': 'kling',
                           'type': ['video'],
                           'concurrency': 10,
                           'price_per_second': 0.5,
                           'capabilities': {'ability_type': 'image_to_video',
                                            'ability_types': ['text_to_video',
                                                              'image_to_video',
                                                              'start_end_frame_i2v',
                                                              'native_audio'],
                                            'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                            'input_modalities': ['text', 'image'],
                                            'adapter_input_modalities': ['text', 'image'],
                                            'api_contract_verified': True}},
            'kling-v2-5-turbo': {'name': 'Kling V2.5 Turbo',
                                 'provider': 'kling',
                                 'type': ['video'],
                                 'concurrency': 10,
                                 'price_per_second': 0.3,
                                 'capabilities': {'ability_type': 'image_to_video',
                                                  'ability_types': ['image_to_video'],
                                                  'adapter_ability_types': ['first_frame_i2v', 'native_audio'],
                                                  'input_modalities': ['text', 'image'],
                                                  'adapter_input_modalities': ['text', 'image'],
                                                  'api_contract_verified': True}},
            'doubao-seedance-2-0-260128': {'name': 'Seedance 2.0',
                                           'provider': 'ark',
                                           'family': 'seedance',
                                           'type': ['video'],
                                           'concurrency': 10,
                                           'price_per_second': 0.5,
                                           'capabilities': {'ability_type': 'image_to_video',
                                                            'ability_types': ['text_to_video', 'image_to_video'],
                                                            'adapter_ability_types': ['first_frame_i2v',
                                                                                      'native_audio'],
                                                            'input_modalities': ['text', 'image'],
                                                            'adapter_input_modalities': ['text', 'image'],
                                                            'duration': {'min': 2,
                                                                         'max': 12,
                                                                         'integer': True,
                                                                         'verified': True},
                                                            'resolutions': ['720p', '1080p'],
                                                            'ratios': ['16:9',
                                                                       '4:3',
                                                                       '1:1',
                                                                       '3:4',
                                                                       '9:16',
                                                                       '21:9',
                                                                       'adaptive'],
                                                            'api_contract_verified': True}},
            'doubao-seedance-2-0-fast-260128': {'name': 'Seedance 2.0 Fast',
                                                'provider': 'ark',
                                                'family': 'seedance',
                                                'type': ['video'],
                                                'concurrency': 10,
                                                'price_per_second': 0.3,
                                                'capabilities': {'ability_type': 'image_to_video',
                                                                 'ability_types': ['text_to_video',
                                                                                   'image_to_video',
                                                                                   'fast_generation'],
                                                                 'adapter_ability_types': ['first_frame_i2v',
                                                                                           'native_audio'],
                                                                 'input_modalities': ['text', 'image'],
                                                                 'adapter_input_modalities': ['text', 'image'],
                                                                 'duration': {'min': 2,
                                                                              'max': 12,
                                                                              'integer': True,
                                                                              'verified': True},
                                                                 'resolutions': ['720p', '1080p'],
                                                                 'ratios': ['16:9',
                                                                            '4:3',
                                                                            '1:1',
                                                                            '3:4',
                                                                            '9:16',
                                                                            '21:9',
                                                                            'adaptive'],
                                                                 'api_contract_verified': True}},
            'doubao-seedance-2-0-mini-260615': {'name': 'Seedance 2.0 Mini',
                                                'provider': 'ark',
                                                'family': 'seedance',
                                                'type': ['video'],
                                                'concurrency': 10,
                                                'price_per_second': 0.2,
                                                'capabilities': {'ability_type': 'image_to_video',
                                                                 'ability_types': ['text_to_video',
                                                                                   'image_to_video',
                                                                                   'fast_generation'],
                                                                 'adapter_ability_types': ['first_frame_i2v',
                                                                                           'native_audio'],
                                                                 'input_modalities': ['text', 'image'],
                                                                 'adapter_input_modalities': ['text', 'image'],
                                                                 'duration': {'min': 2,
                                                                              'max': 12,
                                                                              'integer': True,
                                                                              'verified': True},
                                                                 'resolutions': ['720p', '1080p'],
                                                                 'ratios': ['16:9',
                                                                            '4:3',
                                                                            '1:1',
                                                                            '3:4',
                                                                            '9:16',
                                                                            '21:9',
                                                                            'adaptive'],
                                                                 'api_contract_verified': True}},
            'wan2.7-r2v': {'name': 'Wan 2.7 R2V',
                           'provider': 'dashscope',
                           'family': 'wan',
                           'type': ['video'],
                           'concurrency': 5,
                           'price_per_second': 0.8,
                           'capabilities': {'ability_type': 'reference_to_video',
                                            'ability_types': ['reference_to_video',
                                                              'digital_human',
                                                              'multi_character',
                                                              'native_audio',
                                                              'voice_reference',
                                                              'multi_shot'],
                                            'adapter_ability_types': ['reference_to_video',
                                                                      'digital_human',
                                                                      'voice_reference'],
                                            'input_modalities': ['text', 'image', 'audio', 'video'],
                                            'adapter_input_modalities': ['text', 'image', 'audio'],
                                            'duration': {'min': 2, 'max': 10, 'integer': True, 'verified': True},
                                            'resolutions': ['720P', '1080P'],
                                            'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4'],
                                            'api_contract_verified': True}},
            'wan2.7-videoedit': {'name': 'Wan 2.7 Video Edit',
                                 'provider': 'dashscope',
                                 'family': 'wan',
                                 'type': ['video'],
                                 'concurrency': 5,
                                 'price_per_second': 0.8,
                                 'capabilities': {'ability_type': 'video_editing',
                                                  'ability_types': ['video_editing',
                                                                    'action_transfer',
                                                                    'instruction_editing',
                                                                    'video_transfer'],
                                                  'adapter_ability_types': ['action_transfer', 'video_editing'],
                                                  'input_modalities': ['text', 'image', 'video'],
                                                  'adapter_input_modalities': ['text', 'image', 'video'],
                                                  'duration': {'min': 2, 'max': 10, 'integer': True, 'verified': True},
                                                  'resolutions': ['720P', '1080P'],
                                                  'ratios': ['16:9', '9:16', '1:1', '4:3', '3:4'],
                                                  'api_contract_verified': True}},
            'happyhorse-1.0-r2v': {'name': 'Happy Horse 1.0 R2V',
                                   'provider': 'dashscope',
                                   'family': 'happyhorse',
                                   'type': ['video'],
                                   'concurrency': 5,
                                   'price_per_second': 1.0,
                                   'capabilities': {'ability_type': 'reference_to_video',
                                                    'ability_types': ['reference_to_video',
                                                                      'digital_human',
                                                                      'multi_character',
                                                                      'native_audio',
                                                                      'multi_shot'],
                                                    'adapter_ability_types': ['reference_to_video', 'digital_human'],
                                                    'input_modalities': ['text', 'image'],
                                                    'adapter_input_modalities': ['text', 'image'],
                                                    'duration': {'min': 3,
                                                                 'max': 15,
                                                                 'integer': True,
                                                                 'verified': True},
                                                    'resolutions': ['720P', '1080P'],
                                                    'ratios': ['16:9', '9:16', '3:4', '4:3', '1:1'],
                                                    'api_contract_verified': True}},
            'happyhorse-1.1-r2v': {'name': 'Happy Horse 1.1 R2V',
                                   'provider': 'dashscope',
                                   'family': 'happyhorse',
                                   'type': ['video'],
                                   'concurrency': 5,
                                   'price_per_second': 1.0,
                                   'capabilities': {'ability_type': 'reference_to_video',
                                                    'ability_types': ['reference_to_video',
                                                                      'digital_human',
                                                                      'multi_character',
                                                                      'native_audio',
                                                                      'multi_shot'],
                                                    'adapter_ability_types': ['reference_to_video', 'digital_human'],
                                                    'input_modalities': ['text', 'image'],
                                                    'adapter_input_modalities': ['text', 'image'],
                                                    'duration': {'min': 3,
                                                                 'max': 15,
                                                                 'integer': True,
                                                                 'verified': True},
                                                    'resolutions': ['720P', '1080P'],
                                                    'ratios': ['16:9', '9:16', '3:4', '4:3', '1:1'],
                                                    'api_contract_verified': True}},
            'happyhorse-1.0-video-edit': {'name': 'Happy Horse 1.0 Video Edit',
                                          'provider': 'dashscope',
                                          'family': 'happyhorse',
                                          'type': ['video'],
                                          'concurrency': 5,
                                          'price_per_second': 1.0,
                                          'capabilities': {'ability_type': 'video_editing',
                                                           'ability_types': ['video_editing',
                                                                             'action_transfer',
                                                                             'instruction_editing',
                                                                             'native_audio'],
                                                           'adapter_ability_types': ['action_transfer',
                                                                                     'video_editing'],
                                                           'input_modalities': ['text', 'image', 'video'],
                                                           'adapter_input_modalities': ['text', 'image', 'video'],
                                                           'duration': {'min': 3,
                                                                        'max': 15,
                                                                        'integer': True,
                                                                        'verified': True},
                                                           'resolutions': ['720P', '1080P'],
                                                           'api_contract_verified': True}},
            'seedance-1-0-pro': {'name': 'Seedance 1.0 Pro',
                                 'provider': 'ark',
                                 'family': 'seedance',
                                 'type': ['video'],
                                 'concurrency': 10,
                                 'price_per_second': 0.5,
                                 'capabilities': {'ability_type': 'image_to_video',
                                                  'ability_types': [],
                                                  'adapter_ability_types': ['first_frame_i2v'],
                                                  'api_contract_verified': False}},
            'seedance-1-0-lite': {'name': 'Seedance 1.0 Lite',
                                  'provider': 'ark',
                                  'family': 'seedance',
                                  'type': ['video'],
                                  'concurrency': 10,
                                  'price_per_second': 0.3,
                                  'capabilities': {'ability_type': 'image_to_video',
                                                   'ability_types': [],
                                                   'adapter_ability_types': ['first_frame_i2v'],
                                                   'api_contract_verified': False}}}}


def load_model_config() -> dict[str, Any]:
    return MODEL_CONFIG


def get_model_config(model: str) -> dict[str, Any]:
    """Get metadata for one model, with a loose fallback for provider aliases."""
    models = MODEL_CONFIG.get("models", {})
    if model in models:
        return models[model]

    model_lower = model.lower()
    for key, value in models.items():
        if key in model_lower or model_lower in key:
            return value

    return {
        "name": model,
        "provider": "unknown",
        "type": [],
        "concurrency": 3,
    }


class CostEstimate:
    """Pre-call cost range for reference images and video.

    Inputs are the session's chosen model ids, the number of reference
    scenes/assets (one per storyboard segment), and the sum of
    ``segment.total_duration`` for each episode. The result is ``by_episode``,
    ``total``, and ``unknown_models``.

    Prices are ``price_per_image`` and ``price_per_second`` on the exact
    ``MODEL_CONFIG`` entry. Amounts are unlabeled: the registry has no currency
    field. A model that lacks the price for a quantity it would actually bill
    is listed in ``unknown_models`` and left out of the total. It is never
    priced as zero.
    """

    # Image cost is a range of 1 to 6 calls per asset: _generate_one in
    # character_agent.py loops up to max_iterations = 3, and each pass can
    # call the image API twice through the doctor rewrite.
    MIN_IMAGE_CALLS_PER_ASSET = 1
    MAX_IMAGE_CALLS_PER_ASSET = 6

    # video_agent bills seg.get("total_duration", 10) when the field is absent.
    DEFAULT_SEGMENT_DURATION_SECONDS = 10.0

    @staticmethod
    def estimate(
        model_ids: dict[str, str],
        episodes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Price ``episodes`` of ``{episode_number, asset_count, duration_seconds}``.

        ``model_ids`` uses ``image`` (``price_per_image``) and ``video``
        (``price_per_second``).
        """
        if not isinstance(model_ids, dict):
            model_ids = {}
        if not isinstance(episodes, list):
            episodes = []

        image_model = str(model_ids.get("image") or "").strip()
        video_model = str(model_ids.get("video") or "").strip()
        unknown: list[str] = []
        rows: list[dict[str, Any]] = []
        incomplete = False
        total_min = 0.0
        total_max = 0.0

        for index, episode in enumerate(episodes, start=1):
            if not isinstance(episode, dict):
                continue
            asset_count = CostEstimate._count(episode.get("asset_count"))
            duration = CostEstimate._seconds(episode.get("duration_seconds"))
            image_price = CostEstimate._priced_unit(
                image_model, "price_per_image", asset_count > 0, unknown
            )
            video_price = CostEstimate._priced_unit(
                video_model, "price_per_second", duration > 0, unknown
            )
            image = CostEstimate._image_cost(asset_count, image_price)
            video = CostEstimate._video_cost(duration, video_price)
            episode_total = CostEstimate._sum_costs(image, video)
            if episode_total is None:
                incomplete = True
            else:
                total_min += episode_total["min"]
                total_max += episode_total["max"]
            rows.append({
                "episode_number": episode.get("episode_number", index),
                "asset_count": asset_count,
                "duration_seconds": CostEstimate._money(duration),
                "image": image,
                "video": video,
                "total": episode_total,
            })

        return {
            "by_episode": rows,
            "total": None if incomplete else {
                "min": CostEstimate._money(total_min),
                "max": CostEstimate._money(total_max),
            },
            "unknown_models": unknown,
        }

    @staticmethod
    def from_session(
        meta: dict[str, Any],
        artifacts: dict[str, Any],
        video_model: str,
    ) -> dict[str, Any]:
        """Build the estimate from a session meta dict and its artifacts.

        Reference scenes are the storyboard segments (one image each). When
        the storyboard is missing, ``reference_generation.scenes`` is used and
        contributes no duration. The image model matches reference generation:
        ``image_it2i_model`` when character or setting assets exist to use as
        refs, otherwise ``image_t2i_model``.
        """
        meta = meta if isinstance(meta, dict) else {}
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        return CostEstimate.estimate(
            {
                "image": CostEstimate._reference_image_model(meta, artifacts),
                "video": str(video_model or "").strip(),
            },
            CostEstimate._episodes(artifacts),
        )

    @staticmethod
    def _reference_image_model(meta: dict[str, Any], artifacts: dict[str, Any]) -> str:
        t2i = str(meta.get("image_t2i_model") or "").strip()
        it2i = str(meta.get("image_it2i_model") or "").strip()
        if CostEstimate._has_character_assets(artifacts) and it2i:
            return it2i
        return t2i or it2i

    @staticmethod
    def _has_character_assets(artifacts: dict[str, Any]) -> bool:
        design = artifacts.get("character_design")
        if not isinstance(design, dict):
            return False
        for key in ("characters", "settings"):
            items = design.get(key)
            if isinstance(items, list) and any(isinstance(item, dict) for item in items):
                return True
        return False

    @staticmethod
    def _episodes(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
        storyboard = artifacts.get("storyboard")
        episodes = storyboard.get("episodes") if isinstance(storyboard, dict) else None
        if isinstance(episodes, list) and episodes:
            return CostEstimate._episodes_from_storyboard(episodes)
        reference = artifacts.get("reference_generation")
        scenes = reference.get("scenes") if isinstance(reference, dict) else None
        if isinstance(scenes, list) and scenes:
            return CostEstimate._episodes_from_scenes(scenes)
        return []

    @staticmethod
    def _episodes_from_storyboard(episodes: list) -> list[dict[str, Any]]:
        rows = []
        for index, episode in enumerate(episodes, start=1):
            if not isinstance(episode, dict):
                continue
            segments = episode.get("segments")
            if not isinstance(segments, list):
                segments = []
            asset_count = 0
            duration = 0.0
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                asset_count += 1
                duration += CostEstimate._segment_duration(segment)
            rows.append({
                "episode_number": episode.get("episode_number", index),
                "asset_count": asset_count,
                "duration_seconds": duration,
            })
        return rows

    @staticmethod
    def _segment_duration(segment: dict[str, Any]) -> float:
        if "total_duration" not in segment or segment.get("total_duration") is None:
            return CostEstimate.DEFAULT_SEGMENT_DURATION_SECONDS
        return CostEstimate._seconds(segment.get("total_duration"))

    @staticmethod
    def _episodes_from_scenes(scenes: list) -> list[dict[str, Any]]:
        order: list[Any] = []
        counts: dict[Any, int] = {}
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            episode_number = scene.get("episode", 1)
            if episode_number not in counts:
                order.append(episode_number)
                counts[episode_number] = 0
            counts[episode_number] += 1
        return [
            {
                "episode_number": episode_number,
                "asset_count": counts[episode_number],
                "duration_seconds": 0.0,
            }
            for episode_number in order
        ]

    @staticmethod
    def _priced_unit(
        model_id: str,
        field: str,
        used: bool,
        unknown: list[str],
    ) -> Optional[float]:
        if not used:
            return None
        price = CostEstimate._unit_price(model_id, field)
        if price is None and model_id and model_id not in unknown:
            unknown.append(model_id)
        return price

    @staticmethod
    def _unit_price(model_id: str, field: str) -> Optional[float]:
        if not model_id:
            return None
        metadata = MODEL_CONFIG.get("models", {}).get(model_id)
        if not isinstance(metadata, dict) or metadata.get(field) is None:
            return None
        try:
            return float(metadata[field])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _image_cost(asset_count: int, unit_price: Optional[float]) -> Optional[dict[str, float]]:
        if asset_count <= 0:
            return {"min": 0.0, "max": 0.0}
        if unit_price is None:
            return None
        return {
            "min": CostEstimate._money(
                asset_count * CostEstimate.MIN_IMAGE_CALLS_PER_ASSET * unit_price
            ),
            "max": CostEstimate._money(
                asset_count * CostEstimate.MAX_IMAGE_CALLS_PER_ASSET * unit_price
            ),
        }

    @staticmethod
    def _video_cost(duration_seconds: float, unit_price: Optional[float]) -> Optional[float]:
        if duration_seconds <= 0:
            return 0.0
        if unit_price is None:
            return None
        return CostEstimate._money(duration_seconds * unit_price)

    @staticmethod
    def _sum_costs(
        image: Optional[dict[str, float]],
        video: Optional[float],
    ) -> Optional[dict[str, float]]:
        if image is None or video is None:
            return None
        return {
            "min": CostEstimate._money(image["min"] + video),
            "max": CostEstimate._money(image["max"] + video),
        }

    @staticmethod
    def _count(value: Any) -> int:
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _seconds(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _money(value: float) -> float:
        return round(float(value), 6)


def get_max_concurrency(model: str, enable_concurrency: bool = False) -> int:
    if not enable_concurrency:
        return 1
    return int(get_model_config(model).get("concurrency", 3))


def get_models_by_type(model_type: str) -> list[dict[str, Any]]:
    result = []
    for model_id, metadata in MODEL_CONFIG.get("models", {}).items():
        if model_type in (metadata.get("type") or []):
            result.append({"id": model_id, **metadata})
    return result


def model_type_capabilities(model_type: str, metadata: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Return normalized capability metadata for non-media model selectors."""
    metadata = metadata or {}
    if metadata.get("capabilities"):
        return deepcopy(metadata["capabilities"])
    if model_type == "llm":
        return {
            "ability_type": "text_generation",
            "ability_types": ["text_generation"],
            "adapter_ability_types": ["text_generation"],
            "input_modalities": ["text"],
            "adapter_input_modalities": ["text"],
            "api_contract_verified": bool(metadata.get("api_contract_verified", False)),
        }
    if model_type == "vlm":
        return {
            "ability_type": "vision_language",
            "ability_types": ["vision_language", "image_understanding"],
            "adapter_ability_types": ["vision_language", "image_understanding"],
            "input_modalities": ["text", "image"],
            "adapter_input_modalities": ["text", "image"],
            "api_contract_verified": bool(metadata.get("api_contract_verified", False)),
        }
    return media_capabilities(metadata.get("provider", ""), metadata.get("id", ""), "video" if model_type == "video" else "image")


def model_records(media_type: Optional[str] = None) -> list[dict[str, Any]]:
    records = []
    for model_id, metadata in MODEL_CONFIG.get("models", {}).items():
        resolved_media_type = _resolve_media_type(metadata.get("type") or [])
        if media_type and resolved_media_type != media_type:
            continue
        if resolved_media_type not in {"image", "video"}:
            continue
        records.append(_workflow_info(model_id, metadata, resolved_media_type))
    return records


def list_api_models(
    media_type: Optional[str] = None,
    required_adapter_abilities: Optional[list[str]] = None,
    verified_only: bool = False,
) -> list[dict[str, Any]]:
    records = model_records(media_type=media_type)
    required = set(required_adapter_abilities or [])
    if verified_only:
        records = [record for record in records if record.get("api_contract_verified", True)]
    if required:
        records = [record for record in records if required.intersection(model_ability_tags(record))]
    return records


def parse_api_model(model: str, media_type: str) -> tuple[str, str]:
    if model and model.startswith("api/"):
        _, provider, model_id = model.split("/", 2)
        return provider, model_id

    metadata = MODEL_CONFIG.get("models", {}).get(model)
    if metadata and _resolve_media_type(metadata.get("type") or []) == media_type:
        return metadata.get("provider", ""), model
    return "", model


def media_capabilities(provider: str, model: str, media_type: str) -> dict[str, Any]:
    metadata = MODEL_CONFIG.get("models", {}).get(model, {})
    capabilities = metadata.get("capabilities")
    if capabilities:
        return deepcopy(capabilities)
    if media_type == "image":
        return {
            "ability_type": "image_generation",
            "ability_types": ["text_to_image"],
            "adapter_ability_types": ["text_to_image"],
            "input_modalities": ["text"],
            "adapter_input_modalities": ["text"],
            "api_contract_verified": False,
        }
    return {
        "ability_type": "image_to_video",
        "ability_types": [],
        "adapter_ability_types": ["first_frame_i2v"],
        "input_modalities": ["text", "image"],
        "adapter_input_modalities": ["text", "image"],
        "api_contract_verified": False,
        "duration": {"min": 3, "max": 15, "integer": True, "verified": False},
    }


def video_capabilities(provider: str, model: str) -> dict[str, Any]:
    return media_capabilities(provider, model, "video")


def image_capabilities(provider: str, model: str) -> dict[str, Any]:
    return media_capabilities(provider, model, "image")


def model_ability_tags(record: dict[str, Any]) -> set[str]:
    tags = set(record.get("adapter_ability_types") or [])
    tags.update(record.get("ability_types") or [])
    if record.get("ability_type"):
        tags.add(record["ability_type"])
    tags.update(record.get("type") or [])
    return tags


def _workflow_info(model_id: str, metadata: dict[str, Any], media_type: str) -> dict[str, Any]:
    provider = metadata.get("provider", "")
    capabilities = media_capabilities(provider, model_id, media_type)
    return {
        "key": f"api/{provider}/{model_id}",
        "name": model_id,
        "display_name": f"{metadata.get('name') or model_id} - API {provider.title()}",
        "source": "api",
        "provider": provider,
        "family": metadata.get("family"),
        "model": model_id,
        "media_type": media_type,
        "type": metadata.get("type", []),
        "capabilities": capabilities,
        "ability_type": capabilities.get("ability_type"),
        "ability_types": capabilities.get("ability_types", []),
        "adapter_ability_types": capabilities.get("adapter_ability_types", []),
        "input_modalities": capabilities.get("input_modalities", []),
        "adapter_input_modalities": capabilities.get("adapter_input_modalities", []),
        "api_contract_verified": capabilities.get("api_contract_verified", False),
        "concurrency": metadata.get("concurrency"),
    }


def _resolve_media_type(types: list[str]) -> str:
    if "video" in types:
        return "video"
    if any(item in types for item in ("t2i", "i2i", "image")):
        return "image"
    return ""
