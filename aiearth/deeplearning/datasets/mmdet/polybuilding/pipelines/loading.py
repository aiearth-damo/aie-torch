import os.path as osp

import mmcv
import numpy as np
import pycocotools.mask as maskUtils

# from mmdet.core import BitmapMasks, PolygonMasks
from .structures import PolygonMasks
from mmdet.datasets.builder import PIPELINES

from shapely.geometry import Polygon
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import cdist


@PIPELINES.register_module()
class LoadBuildingAnnotations:
    """Load multiple types of annotations.

    Args:
        with_bbox (bool): Whether to parse and load the bbox annotation.
             Default: True.
        with_label (bool): Whether to parse and load the label annotation.
            Default: True.
        with_mask (bool): Whether to parse and load the mask annotation.
             Default: False.
        with_seg (bool): Whether to parse and load the semantic segmentation
            annotation. Default: False.
        poly2mask (bool): Whether to convert the instance masks from polygons
            to bitmaps. Default: True.
        denorm_bbox (bool): Whether to convert bbox from relative value to
            absolute value. Only used in OpenImage Dataset.
            Default: False.
        file_client_args (dict): Arguments to instantiate a FileClient.
            See :class:`mmcv.fileio.FileClient` for details.
            Defaults to ``dict(backend='disk')``.
    """

    def __init__(
        self,
        with_bbox=True,
        with_label=True,
        with_mask=False,
        with_seg=False,
        poly2mask=True,
        denorm_bbox=False,
        key_points=False,
        key_points_weight=1,
        num_points=0,
        spline_num=10,
        training=True,
        file_client_args=dict(backend="disk"),
    ):
        self.with_bbox = with_bbox
        self.with_label = with_label
        self.with_mask = with_mask
        self.with_seg = with_seg
        self.poly2mask = poly2mask
        self.denorm_bbox = denorm_bbox
        self.file_client_args = file_client_args.copy()
        self.file_client = None
        self.num_points = num_points
        self.spline_num = spline_num
        self.spline_poly_num = num_points * 10
        self.key_points = key_points
        self.key_points_weight = key_points_weight
        self.training = training

    def _load_bboxes(self, results):
        """Private function to load bounding box annotations.

        Args:
            results (dict): Result dict from :obj:`mmdet.CustomDataset`.

        Returns:
            dict: The dict contains loaded bounding box annotations.
        """

        ann_info = results["ann_info"]
        results["gt_bboxes"] = ann_info["bboxes"].copy()

        if self.denorm_bbox:
            h, w = results["img_shape"][:2]
            bbox_num = results["gt_bboxes"].shape[0]
            if bbox_num != 0:
                results["gt_bboxes"][:, 0::2] *= w
                results["gt_bboxes"][:, 1::2] *= h
            results["gt_bboxes"] = results["gt_bboxes"].astype(np.float32)

        gt_bboxes_ignore = ann_info.get("bboxes_ignore", None)
        if gt_bboxes_ignore is not None:
            results["gt_bboxes_ignore"] = gt_bboxes_ignore.copy()
            results["bbox_fields"].append("gt_bboxes_ignore")
        results["bbox_fields"].append("gt_bboxes")

        gt_is_group_ofs = ann_info.get("gt_is_group_ofs", None)
        if gt_is_group_ofs is not None:
            results["gt_is_group_ofs"] = gt_is_group_ofs.copy()

        return results

    def _load_labels(self, results):
        """Private function to load label annotations.

        Args:
            results (dict): Result dict from :obj:`mmdet.CustomDataset`.

        Returns:
            dict: The dict contains loaded label annotations.
        """

        results["gt_labels"] = results["ann_info"]["labels"].copy()
        return results

    def _poly2mask(self, mask_ann, img_h, img_w):
        """Private function to convert masks represented with polygon to
        bitmaps.

        Args:
            mask_ann (list | dict): Polygon mask annotation input.
            img_h (int): The height of output mask.
            img_w (int): The width of output mask.

        Returns:
            numpy.ndarray: The decode bitmap mask of shape (img_h, img_w).
        """

        if isinstance(mask_ann, list):
            # polygon -- a single object might consist of multiple parts
            # we merge all parts into one mask rle code
            rles = maskUtils.frPyObjects(mask_ann, img_h, img_w)
            rle = maskUtils.merge(rles)
        elif isinstance(mask_ann["counts"], list):
            # uncompressed RLE
            rle = maskUtils.frPyObjects(mask_ann, img_h, img_w)
        else:
            # rle
            rle = mask_ann
        mask = maskUtils.decode(rle)
        return mask

    def process_polygons(self, polygons):
        """Convert polygons to list of ndarray and filter invalid polygons.

        Args:
            polygons (list[list]): Polygons of one instance.

        Returns:
            list[numpy.ndarray]: Processed polygons.
        """

        polygons = [np.array(p) for p in polygons]
        valid_polygons = []
        for polygon in polygons:
            if len(polygon) % 2 == 0 and len(polygon) >= 6:
                valid_polygons.append(polygon)
        return valid_polygons

    def process_polygons_m(self, polygons):
        """Convert polygons to list of ndarray and filter invalid polygons.

        Args:
            polygons (list[list]): Polygons of one instance.

        Returns:
            list[numpy.ndarray]: Processed polygons.
        """

        polygons = [np.array(p) for p in polygons]
        valid_polygons = []
        polygon_weights = []
        for polygon in polygons:
            if len(polygon) % 2 == 0 and len(polygon) >= 6:
                poly = Polygon(polygon.reshape(-1, 2))
                poly = poly.simplify(0.5)
                # import pdb;pdb.set_trace()
                poly = np.array(poly.exterior.coords)
                if (poly[0] == poly[-1]).all():
                    poly = poly[:-1]

                min_y = np.min(poly[:, 1])
                min_idy = np.where(poly[:, 1] == min_y)[0]
                min_idx = np.argmin(poly[:, 0][min_idy])
                min_index = min_idy[min_idx]
                poly = np.roll(poly, -min_index, axis=0)
                _thres = 64 if self.num_points <= 0 else self.num_points
                # import pdb
                # pdb.set_trace()
                if len(poly) > _thres:
                    idxnext_p = (np.arange(len(poly), dtype=np.int32) + 1) % len(poly)
                    poly_next = poly[idxnext_p]
                    edgelen_p = np.sqrt(np.sum((poly_next - poly) ** 2, axis=1))
                    edgeidxsort_p = np.argsort(edgelen_p)
                    edgeidxkeep_k = edgeidxsort_p[len(poly) - _thres :]
                    edgeidxsort_k = np.sort(edgeidxkeep_k)
                    poly = poly[edgeidxsort_k]
                poly_64 = np.zeros((_thres, 2), dtype=np.int)
                poly_weight = np.zeros((_thres, 1), dtype=np.int)
                poly_64[: len(poly)] = poly
                poly_weight[: len(poly)] = 1
                valid_polygons.append(poly_64)
                polygon_weights.append(poly_weight)
        if len(polygon_weights) > 1:
            print("HHHHHHere")
        return valid_polygons, polygon_weights

    def _polygon_area(self, poly):
        """Compute the area of a component of a polygon.

        Using the shoelace formula:
        https://stackoverflow.com/questions/24467972/calculate-area-of-polygon-given-x-y-coordinates

        Args:
            x (ndarray): x coordinates of the component
            y (ndarray): y coordinates of the component

        Return:
            float: the are of the component
        """
        x = poly[:, 0]
        y = poly[:, 1]
        return 0.5 * np.abs(np.dot(x, np.roll(y, 1)) - np.dot(y, np.roll(x, 1)))

    def filter_tiny_polys(self, polys):
        polys_ = []
        for poly in polys:
            x_min, y_min = np.min(poly[:, 0]), np.min(poly[:, 1])
            x_max, y_max = np.max(poly[:, 0]), np.max(poly[:, 1])
            if x_max - x_min >= 1 and y_max - y_min >= 1:
                polys_.append(poly)
        return [poly for poly in polys_ if self._polygon_area(poly) > 5]

    def get_cw_poly(self, poly):
        return poly[::-1] if Polygon(poly).exterior.is_ccw else poly

    def unify_polygons(self, polygons, gt_bbox):
        polygons = [np.array(p).reshape(-1, 2) for p in polygons]
        filtered_polygons = self.filter_tiny_polys(polygons)
        if len(filtered_polygons) == 0:
            xmin, ymin, xmax, ymax = gt_bbox[0], gt_bbox[1], gt_bbox[2], gt_bbox[3]
            tl = np.stack([xmin, ymin])
            bl = np.stack([xmin, ymax])
            br = np.stack([xmax, ymax])
            tr = np.stack([xmax, ymin])
            filtered_polygons = [np.stack([tl, bl, br, tr])]

        valid_polygons = []
        valid_weights = []
        for polygon in polygons:
            sampled_polygon = self.uniformsample(polygon, self.spline_poly_num)
            tt_idx = np.argmin(
                np.power(sampled_polygon - sampled_polygon[0], 2).sum(axis=1)
            )
            valid_polygon = np.roll(sampled_polygon, -tt_idx, axis=0)[
                :: self.spline_num
            ]
            cw_valid_polygon = self.get_cw_poly(valid_polygon)
            unify_origin_polygon = self.unify_origin_polygon(cw_valid_polygon)
            valid_polygons.append(unify_origin_polygon.reshape(-1, 2))
            valid_weights.append(np.ones((self.num_points, 1)))
        return valid_polygons, valid_weights

    def unify_origin_polygon(self, poly):
        new_poly = np.zeros_like(poly)
        xmin = poly[:, 0].min()
        xmax = poly[:, 0].max()
        ymin = poly[:, 1].min()
        ymax = poly[:, 1].max()
        tcx = (xmin + xmax) / 2
        tcy = ymin
        dist = (poly[:, 0] - tcx) ** 2 + (poly[:, 1] - tcy) ** 2
        min_dist_idx = dist.argmin()
        new_poly[: (poly.shape[0] - min_dist_idx)] = poly[min_dist_idx:]
        new_poly[(poly.shape[0] - min_dist_idx) :] = poly[:min_dist_idx]
        return new_poly

    def uniformsample(self, pgtnp_px2, newpnum):  # https://github.com/zju3dv/snake
        pnum, cnum = pgtnp_px2.shape
        assert cnum == 2

        idxnext_p = (np.arange(pnum, dtype=np.int32) + 1) % pnum
        pgtnext_px2 = pgtnp_px2[idxnext_p]
        edgelen_p = np.sqrt(np.sum((pgtnext_px2 - pgtnp_px2) ** 2, axis=1))
        edgeidxsort_p = np.argsort(edgelen_p)

        # two cases
        # we need to remove gt points
        # we simply remove shortest paths
        if pnum > newpnum:
            edgeidxkeep_k = edgeidxsort_p[pnum - newpnum :]
            edgeidxsort_k = np.sort(edgeidxkeep_k)
            pgtnp_kx2 = pgtnp_px2[edgeidxsort_k]
            assert pgtnp_kx2.shape[0] == newpnum
            return pgtnp_kx2
        # we need to add gt points
        # we simply add it uniformly
        else:
            edgenum = np.round(edgelen_p * newpnum / np.sum(edgelen_p)).astype(np.int32)
            for i in range(pnum):
                if edgenum[i] == 0:
                    edgenum[i] = 1

            # after round, it may has 1 or 2 mismatch
            edgenumsum = np.sum(edgenum)
            if edgenumsum != newpnum:
                if edgenumsum > newpnum:
                    id = -1
                    passnum = edgenumsum - newpnum
                    while passnum > 0:
                        edgeid = edgeidxsort_p[id]
                        if edgenum[edgeid] > passnum:
                            edgenum[edgeid] -= passnum
                            passnum -= passnum
                        else:
                            passnum -= edgenum[edgeid] - 1
                            edgenum[edgeid] -= edgenum[edgeid] - 1
                            id -= 1
                else:
                    id = -1
                    edgeid = edgeidxsort_p[id]
                    edgenum[edgeid] += newpnum - edgenumsum

            assert np.sum(edgenum) == newpnum

            psample = []
            for i in range(pnum):
                pb_1x2 = pgtnp_px2[i : i + 1]
                pe_1x2 = pgtnext_px2[i : i + 1]

                pnewnum = edgenum[i]
                wnp_kx1 = (
                    np.arange(edgenum[i], dtype=np.float32).reshape(-1, 1) / edgenum[i]
                )

                pmids = pb_1x2 * (1 - wnp_kx1) + pe_1x2 * wnp_kx1
                psample.append(pmids)

            psamplenp = np.concatenate(psample, axis=0)
            return psamplenp

    def _load_masks(self, results):
        """Private function to load mask annotations.

        Args:
            results (dict): Result dict from :obj:`mmdet.CustomDataset`.

        Returns:
            dict: The dict contains loaded mask annotations.
                If ``self.poly2mask`` is set ``True``, `gt_mask` will contain
                :obj:`PolygonMasks`. Otherwise, :obj:`BitmapMasks` is used.
        """
        # import pdb
        # pdb.set_trace()
        h, w = results["img_info"]["height"], results["img_info"]["width"]
        gt_masks = results["ann_info"]["masks"]
        # draw ground truth annotation during test, uncomment following two lines
        if not self.training:
            results["gt_masks"] = gt_masks
            return results

        if self.poly2mask:
            # gt_poly_masks = BitmapMasks(
            #     [self._poly2mask(mask, h, w) for mask in gt_masks], h, w)
            gt_poly_masks = np.array(
                [self._poly2mask(mask, h, w) for mask in gt_masks], dtype=np.float32
            ).sum(0)
            if gt_poly_masks.shape != (h, w):
                print(gt_poly_masks.shape)
                gt_poly_masks = np.zeros((h, w), dtype=np.float32)
        # else:
        # gt_masks = PolygonMasks(
        #     [self.process_polygons(polygons) for polygons in gt_masks], h,
        #     w)
        _gt_polys = []
        _gt_weights = []
        _gt_keys = []
        # import pdb
        # pdb.set_trace()
        for i, gt_mask in enumerate(gt_masks):
            if self.spline_num > 0:
                gt_bboxes = results["ann_info"]["bboxes"]
                poly, poly_weight = self.unify_polygons(gt_mask, gt_bboxes[i])
                # if self.key_points:
                poly2, poly_weight2 = self.process_polygons_m(gt_mask)
                for _idx in range(len(poly)):
                    _poly = poly[_idx]
                    _poly2 = poly2[_idx]
                    _poly_weight2 = poly_weight2[_idx]
                    _poly2 = _poly2[(_poly_weight2 > 0).reshape(-1)]
                    dis = cdist(_poly, _poly2)
                    matched_row_inds, matched_col_inds = linear_sum_assignment(dis)

                    _poly[matched_row_inds] = _poly2[matched_col_inds]
                    poly[_idx] = _poly
                    _label = np.ones((self.num_points, 1), dtype=int)
                    _label[matched_row_inds] = self.key_points_weight
                    poly_weight[_idx] = _label
            else:
                poly, poly_weight = self.process_polygons_m(gt_mask)
            _gt_polys.append(poly)
            _gt_weights.append(poly_weight)
        # import pdb;pdb.set_trace()
        gt_masks = PolygonMasks(_gt_polys, h, w, poly_weights=_gt_weights)

        """'
        gt_masks = PolygonMasks(
            [self.process_polygons_m(polygons) for polygons in gt_masks], h,
            w)
        """
        results["gt_masks"] = gt_masks
        results["mask_fields"].append("gt_masks")
        if self.poly2mask:
            results["gt_poly_masks"] = gt_poly_masks
            results["seg_fields"].append("gt_poly_masks")
        return results

    def _load_semantic_seg(self, results):
        """Private function to load semantic segmentation annotations.

        Args:
            results (dict): Result dict from :obj:`dataset`.

        Returns:
            dict: The dict contains loaded semantic segmentation annotations.
        """

        if self.file_client is None:
            self.file_client = mmcv.FileClient(**self.file_client_args)

        filename = osp.join(results["seg_prefix"], results["ann_info"]["seg_map"])
        img_bytes = self.file_client.get(filename)
        results["gt_semantic_seg"] = mmcv.imfrombytes(
            img_bytes, flag="unchanged"
        ).squeeze()
        results["seg_fields"].append("gt_semantic_seg")
        return results

    def __call__(self, results):
        """Call function to load multiple types annotations.

        Args:
            results (dict): Result dict from :obj:`mmdet.CustomDataset`.

        Returns:
            dict: The dict contains loaded bounding box, label, mask and
                semantic segmentation annotations.
        """
        if self.with_bbox:
            results = self._load_bboxes(results)
            if results is None:
                return None
        if self.with_label:
            results = self._load_labels(results)
        if self.with_mask:
            results = self._load_masks(results)
        if self.with_seg:
            results = self._load_semantic_seg(results)
        return results

    def __repr__(self):
        repr_str = self.__class__.__name__
        repr_str += f"(with_bbox={self.with_bbox}, "
        repr_str += f"with_label={self.with_label}, "
        repr_str += f"with_mask={self.with_mask}, "
        repr_str += f"with_seg={self.with_seg}, "
        repr_str += f"poly2mask={self.poly2mask}, "
        repr_str += f"poly2mask={self.file_client_args})"
        return repr_str
