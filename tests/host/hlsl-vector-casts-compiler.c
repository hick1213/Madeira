/* Host adapter for the actual repository vkd3d-shader compiler. */
#include <stdio.h>
#include <stdlib.h>
#include "vkd3d_shader.h"

int main(int argc, char **argv)
{
    const struct vkd3d_shader_compile_option options[] =
    {
        {VKD3D_SHADER_COMPILE_OPTION_API_VERSION, VKD3D_SHADER_API_VERSION_1_19},
        {VKD3D_SHADER_COMPILE_OPTION_STRIP_DEBUG, 1},
    };
    struct vkd3d_shader_hlsl_source_info hlsl = {0};
    struct vkd3d_shader_compile_info info = {0};
    struct vkd3d_shader_code bytecode = {0}, assembly = {0};
    char *messages = NULL, *source;
    FILE *file;
    long length;
    int result;

    if ((argc != 4 && argc != 5) || !(file = fopen(argv[1], "rb")))
        return 2;
    if (fseek(file, 0, SEEK_END) || (length = ftell(file)) < 0 || fseek(file, 0, SEEK_SET)
            || !(source = malloc(length + 1)) || fread(source, 1, length, file) != length)
        return 2;
    fclose(file);
    source[length] = 0;
    hlsl.type = VKD3D_SHADER_STRUCTURE_TYPE_HLSL_SOURCE_INFO;
    hlsl.entry_point = argc == 5 ? argv[4] : "main";
    hlsl.profile = "ps_5_0";
    info.type = VKD3D_SHADER_STRUCTURE_TYPE_COMPILE_INFO;
    info.next = &hlsl;
    info.source.code = source;
    info.source.size = length;
    info.source_type = VKD3D_SHADER_SOURCE_HLSL;
    info.target_type = VKD3D_SHADER_TARGET_DXBC_TPF;
    info.options = options;
    info.option_count = sizeof(options) / sizeof(*options);
    info.log_level = VKD3D_SHADER_LOG_INFO;
    info.source_name = argv[1];
    result = vkd3d_shader_compile(&info, &bytecode, &messages);
    if (messages) fputs(messages, stderr);
    vkd3d_shader_free_messages(messages);
    free(source);
    if (result < 0) return 1;
    if (!(file = fopen(argv[2], "wb"))) return 2;
    if (fwrite(bytecode.code, 1, bytecode.size, file) != bytecode.size) return 2;
    fclose(file);
    info.next = NULL;
    info.source = bytecode;
    info.source_type = VKD3D_SHADER_SOURCE_DXBC_TPF;
    info.target_type = VKD3D_SHADER_TARGET_D3D_ASM;
    messages = NULL;
    result = vkd3d_shader_compile(&info, &assembly, &messages);
    if (messages) fputs(messages, stderr);
    vkd3d_shader_free_messages(messages);
    if (result < 0) return 1;
    if (!(file = fopen(argv[3], "wb"))) return 2;
    if (fwrite(assembly.code, 1, assembly.size, file) != assembly.size) return 2;
    fclose(file);
    vkd3d_shader_free_shader_code(&assembly);
    vkd3d_shader_free_shader_code(&bytecode);
    return 0;
}
