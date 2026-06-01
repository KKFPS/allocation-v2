class Solution(object):
    def search(self, nums, target):
        """
        :type nums: List[int]
        :type target: int
        :rtype: int
        """

        ptr0 = 0
        ptr1 = len(nums) - 1

        while ptr1 > ptr0:
            mid = (ptr0 + ptr1) // 2

            if target == nums[mid]:
                return mid
            if target < nums[mid]:
                if nums[ptr0] < nums[mid]:   # left half sorted
                    if 
            else:
        
        return -1
        
            
        