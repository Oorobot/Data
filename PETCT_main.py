from PETCT import *
from utils import *

""" series images的顺序是从肺部上面到下面, .nii.gz的顺序恰好相反，从下面到上面.
    所以, 需要将.nii.gz图像序列进行 reverse 
"""


# 常量
SEG_LABEL_FILES = glob("NewPulmonaryNodule/*/*.nii.gz")
LUNG_BASE_PATH = "PET-CT"
LUNG_SLICE = np.loadtxt(
    fname="lung_slice.csv", dtype=np.uint32, delimiter=",", usecols=(1, 2)
)

# # 将分割标签文件移动到 PET-CT 对应的文件夹中, 并重命名为文件夹名
# new2lod = np.loadtxt(
#     fname="new2lod.csv", dtype=np.uint32, delimiter=",", skiprows=1, usecols=1
# )
# for file in SEG_LABEL_FILES:
#     new_dir = file.split("\\")[1]
#     old_dir = new2lod[int(new_dir) - 1]
#     file_name = str(old_dir).zfill(3) + ".nii.gz"
#     dst = os.path.join(LUNG_BASE_PATH, str(old_dir).zfill(3), file_name)
#     print("scr: ", file, ", dst: ", dst)
#     rename(file, dst)


# reg 数据处理
for seg_file in SEG_LABEL_FILES:

    print("now start process file: ", seg_file)

    seg_file_dir = os.path.dirname(seg_file)
    idx = int(seg_file_dir.split("\\"))

    # new_dir = seg_file.split("\\")[1]
    # old_dir = NEW2OLD[int(new_dir) - 1]

    slice_start, slice_end = LUNG_SLICE[idx - 1]

    series_ct_files = glob(os.path.join(seg_file_dir, "CT*"))
    series_pet_files = glob(os.path.join(seg_file_dir, "PET*"))

    # 读取CT、PET、mask file
    series_ct = read_serises_images(series_ct_files)
    series_pet = read_serises_images(series_pet_files)
    segmentation = sitk.ReadImage(seg_file)

    ct_array = sitk.GetArrayFromImage(series_ct)
    pet_array = sitk.GetArrayFromImage(series_pet)
    seg_array = sitk.GetArrayFromImage(segmentation)

    # 计算肺部切片长度
    slice_length = slice_end - slice_start + 1

    # 取出CT肺部切片, 文件名由000开始编号，故如此切片
    lung_ct_files = series_ct_files[slice_start : slice_end + 1]
    lung_ct_array = ct_array[slice_start : slice_end + 1]

    # 计算肺部切片 HU
    lung_hu = np.zeros((slice_length, 512, 512), dtype=np.float32)
    for i in range(slice_length):
        lung_hu[i] = compute_hounsfield_unit(lung_ct_array[i], lung_ct_files[i])

    # 由于每张PET的SUVbw与该PET tag info相关，所以依次计算出SUVbw，随后将SUVbw变为图片并重采样到CT一样大小
    suvbw = np.zeros(pet_array.shape, np.float32)
    for i in range(pet_array.shape[0]):
        suvbw[i] = compute_SUVbw_in_GE(pet_array[i], series_pet_files[i])

    # 还原suv_bw_img信息
    suvbw_img = sitk.GetImageFromArray(suvbw)
    suvbw_img.SetOrigin(series_pet.GetOrigin())
    suvbw_img.SetSpacing(series_pet.GetSpacing())
    suvbw_img.SetDirection(series_pet.GetDirection())

    # 对suv_bw_img重采样
    suvbw_img = resample(suvbw_img, series_ct, False)
    suvbw = sitk.GetArrayFromImage(suvbw_img)

    # 取出SUV肺部切片, 文件名由000开始编号，故如此切片
    lung_suvbw = suvbw[slice_start : slice_end + 1]

    if ct_array.shape != suvbw.shape or lung_ct_array.shape != lung_suvbw.shape:
        print("ct and pet is not matched!!!")
        continue

    # seg 顺序与 PET、CT 顺序相反
    # reverse seg slice
    seg_array = np.flip(seg_array, axis=0)

    for i in range(slice_length):
        # 有分割图时，进行处理
        cur_seg = seg_array[i]
        if np.max(cur_seg):

            """
                保存数据文件格式：npz
                file_name = (old_dir)_(slice_file_name)_(seg_idx).npz
                total: HU(512x512),SUV(512x512),seg(512x512),suvmax,suvmin.suvmean
                暂时忽略 gen: hu(512x512), suv(512x512), seg(512x512)
                reg: cliped hu(32x32), cliped seg(32x32), suvmax, suvmin, suvmean
            """
            # 当前切片文件名
            cur_file_name = lung_ct_files[i].split("\\")[-1][:-4]
            # 对 ct HU 归一化
            cur_hu = (lung_hu[i] + 1000) / 2000.0
            cur_suvbw = lung_suvbw[i]
            print(
                "%s_%s lung slice file is processing!"
                % (str(idx).zfill(3), cur_file_name)
            )

            # 获取掩码后的CT图像
            masked_CT_1 = np.ma.masked_where(cur_seg == 0, cur_hu)
            masked_CT_2 = np.ma.masked_where(cur_seg == 1, cur_hu)
            save_images(
                [masked_CT_1, masked_CT_2],
                ["mask 1", "mask 2"],
                ["bone", "bone"],
                "process/img/%s_%s_mask.png" % (str(idx).zfill(3), cur_file_name),
            )

            # 由于每张图片可能存在多个病灶，所以需要定位出每个病灶并计算出每个病灶的suv max，min，mean
            contours, hierarchy = cv2.findContours(
                cur_seg.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE
            )

            for idx, contour in enumerate(contours):
                contour = np.squeeze(contour)
                if len(contour.shape) == 1:
                    break
                indices_max = np.max(contour, axis=0)
                indices_min = np.min(contour, axis=0)
                # 计算每个病灶的 suv max, suv mean, suv min
                masked_suv = np.ma.masked_where(cur_seg == 0, cur_suvbw)
                cliped_masked_suv = masked_suv[
                    indices_min[1] : indices_max[1] + 1,
                    indices_min[0] : indices_max[0] + 1,
                ]
                suv_max = np.max(cliped_masked_suv)
                suv_min = np.min(cliped_masked_suv)
                suv_mean = np.mean(cliped_masked_suv)
                # 在CT中，切出每个病灶
                clip_rect = clip_based_boundary(
                    [
                        indices_min[1],  # left
                        indices_min[0],  # upper
                        indices_max[1],  # right
                        indices_max[0],  # lower
                    ],
                    cliped_size=(32, 32),
                )
                cliped_image = cur_hu[
                    clip_rect[0] : clip_rect[2], clip_rect[1] : clip_rect[3]
                ]
                cliped_seg = cur_seg[
                    clip_rect[0] : clip_rect[2], clip_rect[1] : clip_rect[3]
                ]
                if clip_rect[4]:
                    cliped_image = cv2.resize(cliped_image, (32, 32))
                    cliped_seg = cv2.resize(cliped_seg, (32, 32))
                    print("there is one need to resize to 32x32!!")

                # seg 仅保留一个中心的病灶
                cliped_seg_only_one = only_center_contour(cliped_seg, (15.5, 15.5))

                # 保存文件
                save_images(
                    [cliped_image, cliped_seg, cliped_seg_only_one],
                    ["img", "seg", "seg_one"],
                    ["bone", "gray", "gray"],
                    "process/img/%s_%s_%s_cliped.png"
                    % (str(idx).zfill(3), cur_file_name, str(idx).zfill(2),),
                )
                np.savez(
                    "process/reg/%s_%s_%s.npz"
                    % (str(idx).zfill(3), cur_file_name, str(idx).zfill(2),),
                    hu=cliped_image,
                    seg=cliped_seg_only_one,
                    suvmax=suv_max,
                    suvmean=suv_mean,
                    suvmin=suv_min,
                )