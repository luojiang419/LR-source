"""硬件预算：高配置不封顶 12 路，低 CPU／内存不强制扩容。"""
import unittest
from unittest.mock import patch
import archive_resources as resources


class ArchiveResourceTests(unittest.TestCase):
    def test_cpu_capacity_including_more_than_twelve(self):
        for cpu in (1,2,8,20,64,128):
            with self.subTest(cpu=cpu):
                self.assertEqual(resources.worker_budget(cpu,256*resources.GiB,256*resources.GiB),cpu)

    def test_memory_budget_reserves_system_space(self):
        self.assertEqual(resources.worker_budget(32,6*resources.GiB,16*resources.GiB),8)
        self.assertEqual(resources.worker_budget(32,2*resources.GiB,8*resources.GiB),2)
        self.assertEqual(resources.worker_budget(32,256*1024**2,8*resources.GiB),1)
        self.assertEqual(resources.worker_budget(32,0,8*resources.GiB),1)

    def test_unknown_resources_fall_back_to_cpu_or_one(self):
        self.assertEqual(resources.worker_budget(20),20)
        self.assertEqual(resources.worker_budget(None),1)
        self.assertEqual(resources.worker_budget(0,64*resources.GiB,64*resources.GiB),1)

    def test_process_cpu_count_and_affinity_are_respected(self):
        with patch.object(resources.os,'process_cpu_count',return_value=12,create=True),patch.object(resources.os,'sched_getaffinity',return_value={1,2,3},create=True):
            self.assertEqual(resources.available_cpus(),3)

    def test_failed_affinity_query_keeps_detected_cpu_count(self):
        with patch.object(resources.os,'process_cpu_count',return_value=20,create=True),patch.object(resources.os,'sched_getaffinity',side_effect=OSError('不可用'),create=True):
            self.assertEqual(resources.available_cpus(),20)


if __name__=='__main__':unittest.main()
